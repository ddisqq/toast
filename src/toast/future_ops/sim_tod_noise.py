# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

import traitlets

import numpy as np

from ..timing import function_timer

from ..traits import trait_docs, Int, Unicode

from ..fft import FFTPlanReal1DStore

from ..operator import Operator

from ..utils import rate_from_times, Logger, AlignedF64

from .._libtoast import tod_sim_noise_timestream


@function_timer
def sim_noise_timestream(
    realization,
    telescope,
    component,
    obsindx,
    detindx,
    rate,
    firstsamp,
    samples,
    oversample,
    freq,
    psd,
    py=False,
):
    """Generate a noise timestream, given a starting RNG state.

    Use the RNG parameters to generate unit-variance Gaussian samples
    and then modify the Fourier domain amplitudes to match the desired
    PSD.

    The RNG (Threefry2x64 from Random123) takes a "key" and a "counter"
    which each consist of two unsigned 64bit integers.  These four
    numbers together uniquely identify a single sample.  We construct
    those four numbers in the following way:

    key1 = realization * 2^32 + telescope * 2^16 + component
    key2 = obsindx * 2^32 + detindx
    counter1 = currently unused (0)
    counter2 = sample in stream

    counter2 is incremented internally by the RNG function as it calls
    the underlying Random123 library for each sample.

    Args:
        realization (int): the Monte Carlo realization.
        telescope (int): a unique index assigned to a telescope.
        component (int): a number representing the type of timestream
            we are generating (detector noise, common mode noise,
            atmosphere, etc).
        obsindx (int): the global index of this observation.
        detindx (int): the global index of this detector.
        rate (float): the sample rate.
        firstsamp (int): the start sample in the stream.
        samples (int): the number of samples to generate.
        oversample (int): the factor by which to expand the FFT length
            beyond the number of samples.
        freq (array): the frequency points of the PSD.
        psd (array): the PSD values.
        py (bool): if True, use a pure-python implementation.  This is useful
            for testing.  If True, also return the interpolated PSD.

    Returns:
        (array):  the noise timestream.  If py=True, returns a tuple of timestream,
            interpolated frequencies, and interpolated PSD.

    """
    tdata = None
    if py:
        fftlen = 2
        while fftlen <= (oversample * samples):
            fftlen *= 2
        npsd = fftlen // 2 + 1
        norm = rate * float(npsd - 1)

        interp_freq = np.fft.rfftfreq(fftlen, 1 / rate)
        if interp_freq.size != npsd:
            raise RuntimeError(
                "interpolated PSD frequencies do not have expected length"
            )

        # Ensure that the input frequency range includes all the frequencies
        # we need.  Otherwise the extrapolation is not well defined.

        if np.amin(freq) < 0.0:
            raise RuntimeError("input PSD frequencies should be >= zero")

        if np.amin(psd) < 0.0:
            raise RuntimeError("input PSD values should be >= zero")

        increment = rate / fftlen

        if freq[0] > increment:
            raise RuntimeError(
                "input PSD does not go to low enough frequency to "
                "allow for interpolation"
            )

        nyquist = rate / 2
        if np.abs((freq[-1] - nyquist) / nyquist) > 0.01:
            raise RuntimeError(
                "last frequency element does not match Nyquist "
                "frequency for given sample rate: {} != {}".format(freq[-1], nyquist)
            )

        # Perform a logarithmic interpolation.  In order to avoid zero values, we
        # shift the PSD by a fixed amount in frequency and amplitude.

        psdshift = 0.01 * np.amin(psd[(psd > 0.0)])
        freqshift = increment

        loginterp_freq = np.log10(interp_freq + freqshift)
        logfreq = np.log10(freq + freqshift)
        logpsd = np.log10(psd + psdshift)

        interp = si.interp1d(logfreq, logpsd, kind="linear", fill_value="extrapolate")

        loginterp_psd = interp(loginterp_freq)
        interp_psd = np.power(10.0, loginterp_psd) - psdshift

        # Zero out DC value

        interp_psd[0] = 0.0

        scale = np.sqrt(interp_psd * norm)

        # gaussian Re/Im randoms, packed into a complex valued array

        key1 = realization * 4294967296 + telescope * 65536 + component
        key2 = obsindx * 4294967296 + detindx
        counter1 = 0
        counter2 = firstsamp * oversample

        rngdata = rng.random(
            fftlen, sampler="gaussian", key=(key1, key2), counter=(counter1, counter2)
        ).array()

        fdata = np.zeros(npsd, dtype=np.complex)

        # Set the DC and Nyquist frequency imaginary part to zero
        fdata[0] = rngdata[0] + 0.0j
        fdata[-1] = rngdata[npsd - 1] + 0.0j

        # Repack the other values.
        fdata[1:-1] = rngdata[1 : npsd - 1] + 1j * rngdata[-1 : npsd - 1 : -1]

        # scale by PSD
        fdata *= scale

        # inverse FFT
        tdata = np.fft.irfft(fdata)

        # subtract the DC level- for just the samples that we are returning
        offset = (fftlen - samples) // 2

        DC = np.mean(tdata[offset : offset + samples])
        tdata[offset : offset + samples] -= DC
        return (tdata[offset : offset + samples], interp_freq, interp_psd)
    else:
        tdata = AlignedF64(samples)
        tod_sim_noise_timestream(
            realization,
            telescope,
            component,
            obsindx,
            detindx,
            rate,
            firstsamp,
            oversample,
            freq.astype(np.float64),
            psd.astype(np.float64),
            tdata,
        )
        return tdata.array()


@trait_docs
class SimNoise(Operator):
    """Operator which generates noise timestreams.

    This passes through each observation and every process generates data
    for its assigned samples.  The observation unique ID is used in the random
    number generation.  The observation dictionary can optionally include a
    'global_offset' member that might be useful if you are splitting observations and
    want to enforce reproducibility of a given sample, even when using
    different-sized observations.

    """

    # Class traits

    API = Int(0, help="Internal interface version for this operator")

    noise_model = Unicode(
        "noise_model", help="Observation key containing the noise model"
    )

    realization = Int(0, help="The noise realization index")

    component = Int(0, help="The noise component index")

    times = Unicode("times", help="Observation shared key for timestamps")

    out = Unicode("noise", help="Observation detdata key for output noise timestreams")

    @traitlets.validate("realization")
    def _check_realization(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("realization index must be positive")
        return check

    @traitlets.validate("component")
    def _check_component(self, proposal):
        check = proposal["value"]
        if check < 0:
            raise traitlets.TraitError("component index must be positive")
        return check

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._oversample = 2

    @function_timer
    def _exec(self, data, detectors=None, **kwargs):
        log = Logger.get()
        for ob in data.obs:
            # Get the detectors we are using for this observation
            dets = ob.select_local_detectors(detectors)
            if len(dets) == 0:
                # Nothing to do for this observation
                continue

            # Unique observation ID
            obsindx = ob.UID

            # FIXME: we should unify naming of UID / id.
            telescope = ob.telescope.id

            # FIXME:  Every observation has a set of timestamps.  This global
            # offset is specified separately so that opens the possibility for
            # inconsistency.  Perhaps the global_offset should be made a property
            # of the Observation class?
            global_offset = 0
            if "global_offset" in ob:
                global_offset = ob["global_offset"]

            if self.noise_model not in ob:
                msg = "Observation does not contain noise model key '{}'".format(
                    self.noise_model
                )
                log.error(msg)
                raise KeyError(msg)

            nse = ob[self.noise_model]

            # Eventually we'll redistribute, to allow long correlations...
            if ob.comm_row_size != 1:
                msg = "Noise simulation for process grids with multiple ranks in the sample direction not implemented"
                log.error(msg)
                raise NotImplementedError(msg)

            # The previous code verified that a single process has whole
            # detectors within the observation.

            # Create output if it does not exist
            if self.out not in ob:
                ob.detdata.create(self.out, detshape=(), dtype=np.float64)

            (rate, dt, dt_min, dt_max, dt_std) = rate_from_times(
                ob.shared[self.times].data
            )

            for key in nse.keys:
                # Check if noise matching this PSD key is needed
                weight = 0.0
                for det in dets:
                    weight += np.abs(nse.weight(det, key))
                if weight == 0:
                    continue

                # Simulate the noise matching this key
                nsedata = sim_noise_timestream(
                    self.realization,
                    telescope,
                    self.component,
                    obsindx,
                    nse.index(key),
                    rate,
                    ob.local_index_offset + global_offset,
                    ob.n_local_samples,
                    self._oversample,
                    nse.freq(key),
                    nse.psd(key),
                )

                # Add the noise to all detectors that have nonzero weights
                for det in dets:
                    weight = nse.weight(det, key)
                    if weight == 0:
                        continue
                    ob.detdata[self.out][det] += weight * nsedata

            # Release the work space allocated in the FFT plan store.
            #
            # FIXME: the fact that we are doing this begs the question of why bother
            # using the plan store at all?  Only one plan per process, per FFT length
            # should be created.  The memory use of these plans should be small relative
            # to the other timestream memory use except in the case where:
            #
            #  1.  Each process only has a few detectors
            #  2.  There is a broad distribution of observation lengths.
            #
            # If we are in this regime frequently, we should just allocate / free
            # each plan.
            store = FFTPlanReal1DStore.get()
            store.clear()
        return

    def _finalize(self, data, **kwargs):
        return

    def _requires(self):
        return {
            "meta": [
                self.noise_model,
            ],
            "shared": [
                self.times,
            ],
        }

    def _provides(self):
        return {
            "detdata": [
                self.out,
            ]
        }

    def _accelerators(self):
        return list()
