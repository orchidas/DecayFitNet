import torch
import torch.nn as nn
from torch.utils.data import Dataset
import torchaudio
import scipy
import scipy.stats
import scipy.signal
import numpy as np
import matplotlib.pyplot as plt
from typing import Iterable, Tuple, TypeVar, Callable, Any, List, Dict
import h5py

T = TypeVar('T', bound=Callable[..., Any])


# https://realpython.com/documenting-python-code/

class DecayDataset(Dataset):
    """Decay dataset."""

    def __init__(self, n_slopes_min=1, n_slopes_max=5, edcs_per_slope=10000, triton_flag=False, testset_flag=False,
                 testset_name='summer830'):
        """
        Args:
        """
        self.testset_flag = testset_flag

        if triton_flag:
            datasets_dir = '/scratch/elec/t40527-hybridacoustics/datasets/decayfitting/'
        else:
            datasets_dir = '/Volumes/ARTSRAM/GeneralDecayEstimation/decayFitting/'

        if not testset_flag:
            # Load EDCs
            f_edcs = h5py.File(datasets_dir + 'edcs_slim.mat', 'r')
            edcs = np.array(f_edcs.get('edcs'))

            # Load noise values
            f_noise_levels = h5py.File(datasets_dir + 'noiseLevels_slim.mat', 'r')
            noise_levels = np.array(f_noise_levels.get('noiseLevels'))

            # Get EDCs into pytorch format
            edcs = torch.from_numpy(edcs).float()
            self.edcs = edcs[:, (n_slopes_min - 1) * edcs_per_slope:n_slopes_max * edcs_per_slope]

            # Put EDCs into dB
            edcs_db = 10*torch.log10(self.edcs)
            assert not torch.any(torch.isnan(edcs_db)), 'NaN values in db EDCs'

            # Normalize dB values to lie between -1 and 1 (input scaling)
            self.edcs_db_normfactor = torch.max(torch.abs(edcs_db))
            edcs_db_normalized = 2 * edcs_db / self.edcs_db_normfactor
            edcs_db_normalized += 1

            assert not torch.any(torch.isnan(edcs_db_normalized)), 'NaN values in normalized EDCs'
            assert not torch.any(torch.isinf(edcs_db_normalized)), 'Inf values in normalized EDCs'
            self.edcs_db_normalized = edcs_db_normalized

            # Generate vector that specifies how many slopes are in every EDC
            self.n_slopes = torch.zeros((1, self.edcs.shape[1]))
            for slope_idx in range(n_slopes_min, n_slopes_max+1):
                self.n_slopes[0, (slope_idx - 1) * edcs_per_slope:slope_idx * edcs_per_slope] = slope_idx - 1
            self.n_slopes = self.n_slopes.long()

            # Noise level values are used in training for the noise loss
            noise_levels = torch.from_numpy(noise_levels).float()
            self.noise_levels = noise_levels[:, (n_slopes_min-1)*edcs_per_slope:n_slopes_max*edcs_per_slope]

            assert self.edcs.shape[1] == self.noise_levels.shape[1], 'More EDCs than noise_levels'
        else:
            if testset_name == 'summer830':
                f_edcs = h5py.File(datasets_dir + 'summer830/edcs_slim.mat', 'r')
                edcs = torch.from_numpy(np.array(f_edcs.get('summer830edcs/edcs'))).float().view(-1, 100).T
            elif testset_name == 'roomtransition':
                f_edcs = h5py.File(datasets_dir + 'roomtransition/edcs_slim.mat', 'r')
                edcs = torch.from_numpy(np.array(f_edcs.get('roomTransitionEdcs/edcs'))).float().view(-1, 100).T
            else:
                raise NotImplementedError('Unknown testset.')

            self.edcs = edcs

    def __len__(self):
        return self.edcs.shape[1]

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        if self.testset_flag:
            edcs = self.edcs[:, idx]
            return edcs
        else:
            edcs = self.edcs[:, idx]
            edcs_db_normalized = self.edcs_db_normalized[:, idx]
            noise_levels = self.noise_levels[:, idx]
            n_slopes = self.n_slopes[:, idx]

            return edcs, noise_levels, edcs_db_normalized, n_slopes


class DecayFitNetLinear(nn.Module):
    def __init__(self, n_slopes, n_max_units, n_filters, n_layers, relu_slope, dropout, reduction_per_layer, device):
        super(DecayFitNetLinear, self).__init__()

        self.n_slopes = n_slopes
        self.device = device

        self.activation = nn.LeakyReLU(relu_slope)
        self.dropout = nn.Dropout(dropout)

        # Base Network
        self.conv1 = nn.Conv1d(1, n_filters, kernel_size=13, padding=6)
        self.maxpool1 = nn.MaxPool1d(5)
        self.conv2 = nn.Conv1d(n_filters, n_filters*2, kernel_size=7, padding=3)
        self.maxpool2 = nn.MaxPool1d(5)
        self.conv3 = nn.Conv1d(n_filters*2, n_filters*2, kernel_size=7, padding=3)
        self.maxpool3 = nn.MaxPool1d(2)
        self.input = nn.Linear(2*n_filters*2, n_max_units)

        self.linears = nn.ModuleList([nn.Linear(round(n_max_units * (reduction_per_layer**i)),
                                                round(n_max_units * (reduction_per_layer**(i+1)))) for i in range(n_layers-1)])

        # T_vals
        self.final1_t = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers - 1))), 50)
        self.final2_t = nn.Linear(50, n_slopes)

        # A_vals
        self.final1_a = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers-1))), 50)
        self.final2_a = nn.Linear(50, n_slopes)

        # Noise
        self.final1_n = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers-1))), 50)
        self.final2_n = nn.Linear(50, 1)

        # N Slopes
        self.final1_n_slopes = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers-1))), 50)
        self.final2_n_slopes = nn.Linear(50, n_slopes)

    def forward(self, edcs):
        """
        Args:

        Returns:
        """

        # Base network
        x = self.maxpool1(self.activation(self.conv1(edcs.unsqueeze(1))))
        x = self.maxpool2(self.activation(self.conv2(x)))
        x = self.maxpool3(self.activation(self.conv3(x)))
        x = self.activation(self.input(self.dropout(x.view(edcs.shape[0], -1))))
        for layer in self.linears:
            x = layer(x)
            x = self.activation(x)

        # T_vals
        t = self.activation(self.final1_t(x))
        t = torch.pow(self.final2_t(t), 2.0) + 0.01

        # A_vals
        a = self.activation(self.final1_a(x))
        a = torch.pow(self.final2_a(a), 2.0) + 1e-16

        # Noise
        n_exponent = self.activation(self.final1_n(x))
        n_exponent = self.final2_n(n_exponent)

        # N Slopes
        n_slopes = self.activation(self.final1_n_slopes(x))
        n_slopes = self.final2_n_slopes(n_slopes)

        return t, a, n_exponent, n_slopes



class DecayFitNetLinearExactlyNSlopes(nn.Module):
    def __init__(self, n_slopes, n_max_units, n_filters, n_layers, relu_slope, dropout, reduction_per_layer, device):
        super(DecayFitNetLinearExactlyNSlopes, self).__init__()

        self.n_slopes = n_slopes
        self.device = device

        self.activation = nn.LeakyReLU(relu_slope)
        self.dropout = nn.Dropout(dropout)

        # Base Network
        self.conv1 = nn.Conv1d(1, n_filters, kernel_size=13, padding=6)
        self.maxpool1 = nn.MaxPool1d(10)
        self.conv2 = nn.Conv1d(n_filters, n_filters*2, kernel_size=7, padding=3)
        self.maxpool2 = nn.MaxPool1d(8)
        self.conv3 = nn.Conv1d(n_filters*2, n_filters*4, kernel_size=7, padding=3)
        self.maxpool3 = nn.MaxPool1d(6)
        self.input = nn.Linear(5*n_filters*4, n_max_units)

        self.linears = nn.ModuleList([nn.Linear(round(n_max_units * (reduction_per_layer**i)),
                                                round(n_max_units * (reduction_per_layer**(i+1)))) for i in range(n_layers-1)])

        # T_vals
        self.final1_t = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers - 1))), 50)
        self.final2_t = nn.Linear(50, n_slopes)

        # A_vals
        self.final1_a = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers-1))), 50)
        self.final2_a = nn.Linear(50, n_slopes)

        # Noise
        self.final1_n = nn.Linear(round(n_max_units * (reduction_per_layer ** (n_layers-1))), 50)
        self.final2_n = nn.Linear(50, 1)

    def forward(self, edcs):
        """
        Args:

        Returns:
        """

        # Base network
        x = self.maxpool1(self.activation(self.conv1(edcs.unsqueeze(1))))
        x = self.maxpool2(self.activation(self.conv2(x)))
        x = self.maxpool3(self.activation(self.conv3(x)))
        x = self.activation(self.input(self.dropout(x.view(edcs.shape[0], -1))))
        for layer in self.linears:
            x = layer(x)
            x = self.activation(x)

        # T_vals
        t = self.activation(self.final1_t(x))
        t = torch.pow(self.final2_t(t), 2.0) + 0.01

        # A_vals
        a = self.activation(self.final1_a(x))
        a = torch.pow(self.final2_a(a), 2.0) + 1e-16

        # Noise
        n_exponent = self.activation(self.final1_n(x))
        n_exponent = self.final2_n(n_exponent)

        return t, a, n_exponent


class FilterByOctaves(nn.Module):
    """Generates an octave wide filterbank and filters tensors.

    This is gpu compatible if using torch backend, but it is super slow and should not be used at all.
    The octave filterbanks is created using cascade Buttwerworth filters, which then are processed using
    the biquad function native to PyTorch.

    This is useful to get the decay curves of RIRs.
    """

    def __init__(self, center_freqs=[125, 250, 500, 1000, 2000, 4000], order=3, fs=48000, backend='scipy'):
        super(FilterByOctaves, self).__init__()

        self.center_freqs = center_freqs
        self.order = order
        self.fs = fs
        self.backend = backend
        self.sos = []
        for freq in self.center_freqs:
            tmp_sos = self._get_octave_filter(freq, self.fs, self.order)
            self.sos.append(tmp_sos)

    ## TODO remove torch back end?
    def _forward_torch(self, x):
        out = []
        for ii, this_sos in enumerate(self.sos):
            tmp = torch.clone(x)
            for jj in range(this_sos.shape[0]):
                tmp = torchaudio.functional.biquad(tmp,
                                                   b0=this_sos[jj, 0], b1=this_sos[jj, 1], b2=this_sos[jj, 2],
                                                   a0=this_sos[jj, 3], a1=this_sos[jj, 4], a2=this_sos[jj, 5])
            out.append(torch.clone(tmp))
        out = torch.stack(out, dim=-2)  # Stack over frequency bands

        return out

    def _forward_scipy(self, x):
        out = []
        for ii, this_sos in enumerate(self.sos):
            tmp = torch.clone(x).cpu().numpy()
            tmp = scipy.signal.sosfilt(this_sos, tmp, axis=-1)
            out.append(torch.from_numpy(tmp))
        out = torch.stack(out, dim=-2)  # Stack over frequency bands

        return out

    def forward(self, x):
        if self.backend == 'scipy':
            out = self._forward_scipy(x)
        else:
            out = self._forward_torch(x)
        return out

    def get_filterbank_impulse_response(self):
        """Returns the impulse response of the filterbank."""
        impulse = torch.zeros(1, self.fs * 20)
        impulse[0, self.fs] = 1
        response = self.forward(impulse)
        return response

    @staticmethod
    def _get_octave_filter(center_freq: float, fs: int, order: int = 3) -> torch.Tensor:
        """
        Design octave band filters with butterworth.
        Returns a sos matrix (tensor) of the shape [filters, 6], in standard sos format.

        Based on octdsgn(Fc,Fs,N); in MATLAB.
        References:
            [1] ANSI S1.1-1986 (ASA 65-1986): Specifications for
                Octave-Band and Fractional-Octave-Band Analog and
                Digital Filters, 1993.
        """
        beta = np.pi / 2 / order / np.sin(np.pi / 2 / order)
        alpha = (1 + np.sqrt(1 + 8 * beta ** 2)) / 4 / beta
        W1 = center_freq / (fs / 2) * np.sqrt(1 / 2) / alpha
        W2 = center_freq / (fs / 2) * np.sqrt(2) * alpha
        Wn = np.array([W1, W2])

        sos = scipy.signal.butter(N=order, Wn=Wn, btype='bandpass', analog=False, output='sos')
        return torch.from_numpy(sos)


def _tupleset(t: Iterable[T], i: int, value: T) -> Tuple[T, ...]:
    lst = list(t)
    lst[i] = value
    return tuple(lst)


def _cumtrapz(y: torch.Tensor,
              x: np.ndarray = None,
              device: str = 'cpu',
              axis: int = -1, ) -> torch.Tensor:
    """
    Cumulative trapezoid integral in PyTorch.
    Heavily based on the scipy implementation here:
    https://github.com/pytorch/pytorch/issues/52552
    """
    if x is None:
        d = np.asarray([1.0])
    else:
        if x.ndim == 1:
            d = np.diff(x)
            # reshape to correct shape
            shape = [1] * y.ndim
            shape[axis] = -1
            d = d.reshape(shape)
        elif len(x.shape) != len(y.shape):
            raise ValueError("If given, shape of x must be 1-D or the "
                             "same as y.")
        else:
            d = np.diff(x, axis=axis)

        if d.shape[axis] != y.shape[axis] - 1:
            raise ValueError("If given, length of x along axis must be the "
                             "same as y.")

    d = torch.from_numpy(d).to(device)

    nd = len(y.shape)
    slice1 = _tupleset((slice(None),) * nd, axis, slice(1, None))
    slice2 = _tupleset((slice(None),) * nd, axis, slice(None, -1))
    res = torch.cumsum(d * (y[slice1] + y[slice2]) / 2.0, dim=axis).to(device)

    shape = list(res.shape)
    shape[axis] = 1
    res = torch.cat([torch.zeros(shape, dtype=res.dtype).to(device), res], dim=axis)

    return res


class Normalizer(torch.nn.Module):
    """ Normalizes the data to have zero mean and unit variance for each feature."""

    def __init__(self, means, stds):
        super(Normalizer, self).__init__()
        self.means = means
        self.stds = stds
        self.eps = np.finfo(np.float32).eps

    def forward(self, x):
        out = x - self.means
        out = out / (self.stds + self.eps)

        return out


def discard_last5(edc: torch.Tensor) -> torch.Tensor:
    # Discard last 5%
    last_id = int(np.round(0.95 * edc.shape[-1]))
    out = edc[..., 0:last_id]

    return out


class PreprocessRIR(nn.Module):
    """ Preprocess a RIR to extract the EDC and prepare it for the neural network model.
        The preprocessing includes: (Upated 24.06.2021):

        # RIR -> Filterbank -> octave-band filtered RIR
        # octave-band filtered RIR -> backwards integration -> EDC
        # EDC -> delete last 5% of samples -> EDC_crop
        # EDC_crop -> downsample to the smallest number above 2400, i.e. by factor floor(original_length / 2400)
            -> EDC_ds1
        # EDC_ds1 -> as it might still be a little more than 2400 samples, just cut away everything after 2400 samples
            -> EDC_ds2
        # EDC_ds2 -> dB scale-> EDC_db
        # EDC_db -> normalization -> EDC_final that is the input to the network
    """

    def __init__(self,
                 input_transform: Dict,
                 normalization: bool = True,
                 sample_rate: int = 48000,
                 filter_frequencies: List[int] = [125, 250, 500, 1000, 2000, 4000], output_size: int = 100):
        super(PreprocessRIR, self).__init__()
        self.input_transform = input_transform
        self.filter_frequencies = filter_frequencies
        self.output_size = output_size
        self.sample_rate = sample_rate
        self.normalization = normalization
        self.eps = 1e-10

        self.filterbank = FilterByOctaves(center_freqs=filter_frequencies, order=3, fs=self.sample_rate,
                                          backend='scipy')

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.schroeder(x)
        out = discard_last5(out)

        # Discard all beyond -140
        # TODO: I am skipping this part due to tensor processing

        # Downsample
        out = torch.nn.functional.interpolate(out, size=self.output_size, scale_factor=None, mode='linear',
                                              align_corners=False, recompute_scale_factor=None)

        # Convert to dB
        out = 10 * torch.log10(out + self.eps)
        # Clamp to -140 dB
        out = torch.clamp_min(out, -140)

        if self.normalization:
            out = 2 * out / self.input_transform["edcs_db_normfactor"]
            out = out + 1

        # Reshape freq bands as batch size, shape = [batch * freqs, timesteps]
        out = out.view(-1, out.shape[-1]).type(torch.float32)

        return out

    def schroeder(self, rir: torch.Tensor) -> torch.Tensor:
        # Filter
        out = self.filterbank(rir)

        # Backwards integral
        reverse_index = torch.arange(out.shape[-1] - 1, -1, -1)
        out = _cumtrapz(out[..., reverse_index] ** 2, device=out.device)
        reverse_index = torch.arange(out.shape[-1] - 1, -1, -1)
        out = out[..., reverse_index]

        # Normalize to 1
        out = out / torch.max(out, dim=-1, keepdim=True).values  # per channel
        # out = out / torch.max(torch.max(out, dim=-1, keepdim=True).values, dim=-2, keepdim=True).values

        return out


def generate_synthetic_edc(T, A, noiseLevel, t, device) -> torch.Tensor:
    """ Generates an EDC from the estimated parameters."""
    # Calculate decay rates, based on the requirement that after T60 seconds, the level must drop to -60dB
    tau_vals = -torch.log(torch.Tensor([1e-6])).to(device) / T

    # Repeat values such that end result will be (batch_size, n_slopes, sample_idx)
    t_rep = t.repeat(T.shape[0], T.shape[1], 1)
    tau_vals_rep = tau_vals.unsqueeze(2).repeat(1, 1, t.shape[0])

    # Calculate exponentials from decay rates
    time_vals = -t_rep * tau_vals_rep
    exponentials = torch.exp(time_vals)

    # Offset is required to make last value of EDC be correct
    exp_offset = exponentials[:, :, -1].unsqueeze(2).repeat(1, 1, t.shape[0])

    # Repeat values such that end result will be (batch_size, n_slopes, sample_idx)
    A_rep = A.unsqueeze(2).repeat(1, 1, t.shape[0])

    # Multiply exponentials with their amplitudes and sum all exponentials together
    edcs = A_rep * (exponentials - exp_offset)
    edc = torch.sum(edcs, 1)

    # Add noise
    noise = noiseLevel * torch.linspace(len(t), 1, len(t)).to(device)
    edc = edc + noise
    return edc


def postprocess_parameters(t_prediction, a_prediction, n_prediction, n_slopes_probabilities, device, sort_values=True):
    # Clamp noise to reasonable values to avoid numerical problems and go from exponent to actual noise value
    n_prediction = torch.clamp(n_prediction, -32, 32)

    # Go from noise exponent to noise value
    n_prediction = torch.pow(10, n_prediction)

    # Get a binary mask to only use the number of slopes that were predicted, zero others
    _, n_slopes_prediction = torch.max(n_slopes_probabilities, 1)
    n_slopes_prediction += 1  # because python starts at 0
    temp = torch.linspace(1, 3, 3).repeat(n_slopes_prediction.shape[0], 1).to(device)
    mask = temp.less_equal(n_slopes_prediction.unsqueeze(1).repeat(1, 3))
    a_prediction[~mask] = 0

    if sort_values:
        # Sort T and A values:
        # 1) assign nans to sort the inactive slopes to the end
        t_prediction[~mask] = float('nan')  # nan as a placeholder, gets replaced in a few lines
        a_prediction[~mask] = float('nan')  # nan as a placeholder, gets replaced in a few lines

        # 2) sort and set nans to zero again
        t_prediction, sort_idxs = torch.sort(t_prediction)
        for batch_idx, a_this_batch in enumerate(a_prediction):
            a_prediction[batch_idx, :] = a_this_batch[sort_idxs[batch_idx]]
        t_prediction[torch.isnan(t_prediction)] = 0  # replace nan from above
        a_prediction[torch.isnan(a_prediction)] = 0  # replace nan from above

    return t_prediction, a_prediction, n_prediction, n_slopes_prediction


def adjust_timescale(t_prediction, n_prediction, L_EDC, fs):
    # T value predictions have to be adjusted for the time-scale conversion (downsampling)
    t_adjust = 10 / (L_EDC / fs)
    t_prediction = t_prediction / t_adjust

    # N value predictions have to be converted from exponent representation to actual value and adjusted for
    # the downsampling
    n_adjust = L_EDC / 100
    n_prediction = n_prediction / n_adjust

    return t_prediction, n_prediction


def edc_loss(t_vals_prediction, a_vals_prediction, n_exp_prediction, edcs_true, device, training_flag=True,
             plot_fit=False, apply_mean=True):
    fs = 10
    l_edc = 10

    # Generate the t values that would be discarded as well, otherwise the models do not match.
    t = (torch.linspace(0, l_edc * fs - 1, round((1/0.95)*l_edc * fs)) / fs).to(device)

    # Clamp noise to reasonable values to avoid numerical problems and go from exponent to actual noise value
    n_exp_prediction = torch.clamp(n_exp_prediction, -32, 32)
    n_vals_prediction = torch.pow(10, n_exp_prediction)

    if training_flag:
        # use L1Loss in training
        loss_fn = nn.L1Loss(reduction='none')
    else:
        loss_fn = nn.MSELoss(reduction='none')

    # Use predicted values to generate an EDC
    edc_prediction = generate_synthetic_edc(t_vals_prediction, a_vals_prediction, n_vals_prediction, t, device)

    # discard last 5 percent (i.e. the step which is already done for the true EDC and the test datasets prior to
    # saving them to the .mat files that are loaded in the beginning of this script
    edc_prediction = edc_prediction[:, 0:l_edc*fs]

    if plot_fit:
        for idx in range(0, edcs_true.shape[0]):
            plt.plot(10 * torch.log10(edcs_true[idx, :]))
            plt.plot(10 * torch.log10(edc_prediction[idx, :].detach()))
            plt.show()

    # Go to dB scale
    edc_true_db = 10 * torch.log10(edcs_true + 1e-16)
    edc_prediction_db = 10 * torch.log10(edc_prediction + 1e-16)

    # Calculate loss on dB scale
    if apply_mean:
        loss = torch.mean(loss_fn(edc_true_db, edc_prediction_db))
    else:
        loss = loss_fn(edc_true_db, edc_prediction_db)

    return loss
