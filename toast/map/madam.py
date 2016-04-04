# Copyright (c) 2015 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by 
# a BSD-style license that can be found in the LICENSE file.


from mpi4py import MPI

import ctypes as ct
from ctypes.util import find_library

import unittest

import numpy as np
import numpy.ctypeslib as npc

from ..dist import Comm, Data
from ..operator import Operator
from ..tod import TOD
from ..tod import Interval


try:
    libmadam = ct.CDLL('libmadam.so')
except:
    libmadam = None

if libmadam is not None:
    libmadam.destripe.restype = None
    libmadam.destripe.argtypes = [
        ct.c_int,
        ct.c_char_p,
        ct.c_long,
        ct.c_char_p,
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        ct.c_long,
        ct.c_long,
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        npc.ndpointer(dtype=np.int64, ndim=1, flags='C_CONTIGUOUS'),
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        ct.c_long,
        npc.ndpointer(dtype=np.int64, ndim=1, flags='C_CONTIGUOUS'),
        npc.ndpointer(dtype=np.int64, ndim=1, flags='C_CONTIGUOUS'),
        ct.c_long,
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        ct.c_long,
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS'),
        ct.c_long,
        npc.ndpointer(dtype=np.float64, ndim=1, flags='C_CONTIGUOUS')
    ]


class OpMadam(Operator):
    """
    Operator which passes data to libmadam for map-making.

    Args:
        params (dictionary): parameters to pass to madam.
    """

    def __init__(self, flavor=None, pmat=None, detweights=None, purge=True, params={}):
        
        # We call the parent class constructor, which currently does nothing
        super().__init__()
        # madam uses time-based distribution
        self._timedist = True
        self._flavor = flavor
        if self._flavor is None:
            self._flavor = TOD.DEFAULT_FLAVOR
        self._pmat = pmat
        if self._pmat is None:
            self._pmat = TOD.DEFAULT_FLAVOR
        self._detw = detweights
        self._purge = purge
        self._params = params


    @property
    def timedist(self):
        return self._timedist


    @property
    def available(self):
        return (libmadam is not None)
    

    def _dict2parstring(self, d):
        s = ''
        for key, value in d.items():
            s += '{} = {};'.format(key, value)
        return s


    def _dets2detstring(self, dets):
        s = ''
        for d in dets:
            s += '{};'.format(d)
        return s


    def exec(self, data):
        if libmadam is None:
            raise RuntimeError("Cannot find libmadam")

        # the two-level pytoast communicator
        comm = data.comm

        # Madam only works with a data model where there is one observation
        # split among the processes, and where the data is distributed time-wise
        if len(data.obs) != 1:
            raise RuntimeError("Madam requires a single observation")

        tod = data.obs[0]['tod']
        if not tod.timedist:
            raise RuntimeError("Madam requires data to be distributed by time")

        # get the total list of intervals
        intervals = None
        if 'intervals' in data.obs[0].keys():
            intervals = data.obs[0]['intervals']
        if intervals is None:
            intervals = [Interval(start=0.0, stop=0.0, first=0, last=(tod.total_samples-1))]

        # get the noise object
        if 'noise' in data.obs[0].keys():
            nse = data.obs[0]['noise']
        else:
            nse = None

        todcomm = tod.mpicomm
        todfcomm = todcomm.py2f()

        # create madam-compatible buffers

        ndet = len(tod.detectors)
        nlocal = tod.local_samples[1]
        nnz = tod.pmat_nnz(self._pmat, tod.detectors[0])

        parstring = self._dict2parstring(self._params)
        detstring = self._dets2detstring(tod.detectors)

        timestamps = tod.read_times(local_start=0, n=nlocal)

        signal = np.zeros(ndet * nlocal, dtype=np.float64)
        flags = np.zeros(ndet * nlocal, dtype=np.uint8)
        pixels = np.zeros(ndet * nlocal, dtype=np.int64)
        pixweights = np.zeros(ndet * nlocal * nnz, dtype=np.float64)

        for d in range(ndet):
            dslice = slice(d * nlocal, (d+1) * nlocal)
            dwslice = slice(d * nlocal * nnz, (d+1) * nlocal * nnz)
            signal[dslice], flags[dslice] = tod.read(detector=tod.detectors[d], flavor=self._flavor, local_start=0, n=nlocal)
            pixels[dslice], pixweights[dwslice] = tod.read_pmat(name=self._pmat, detector=tod.detectors[d], local_start=0, n=nlocal)
            if self._purge:
                tod.clear(detector=tod.detectors[d], flavor=self._flavor)
                tod.clear_pmat(name=self._pmat, detector=tod.detectors[d])
        
        # apply detector flags to the pointing matrix, since that is the
        # only way to pass flag information to madam
        
        pixels[flags != 0] = -1
        pixweights[np.repeat(flags, nnz) != 0] = 0.0

        # The "pointing periods" we pass to madam are simply the intersection
        # of our local data and the list of valid intervals.

        local_bounds = [ (t.first - tod.local_samples[0]) if (t.first > tod.local_samples[0]) else 0 for t in intervals if (t.last >= tod.local_samples[0]) and (t.first < (tod.local_samples[0] + tod.local_samples[1])) ]
        
        nperiod = len(local_bounds)

        periods = np.zeros(nperiod, dtype=np.int64)
        for p in range(nperiod):
            periods[p] = int(local_bounds[p])

        # detweights is either a dictionary of weights specified at construction time,
        # or else we use uniform weighting.
        detw = {}
        if self._detw is None:
            for d in range(ndet):
                detw[tod.detectors[d]] = 1.0
        else:
            detw = self._detw

        detweights = np.zeros(ndet, dtype=np.float64)
        for d in range(ndet):
            detweights[d] = detw[tod.detectors[d]]

        if nse is not None:
            nse_psdfreqs = nse.freq
            npsdbin = len(nse_psdfreqs)
            psdfreqs = np.copy(nse_psdfreqs)

            npsd = np.ones(ndet, dtype=np.int64)
            npsdtot = np.sum(npsd)
            psdstarts = np.zeros(npsdtot, dtype=np.float64)

            npsdval = npsdbin * npsdtot
            psdvals = np.zeros(npsdval, dtype=np.float64)
            for d in range(ndet):
                psdvals[d*npsdbin:(d+1)*npsdbin] = nse.psd(tod.detectors[d])
        else:
            npsd = np.ones(ndet, dtype=np.int64)
            npsdtot = np.sum(npsd)
            psdstarts = np.zeros(npsdtot)
            npsdbin = 10
            fsample = 10.
            psdfreqs = np.arange(npsdbin) * fsample / npsdbin
            npsdval = npsdbin * npsdtot            
            psdvals = np.ones( npsdval )
            

        # destripe

        libmadam.destripe(todfcomm, parstring.encode(), ndet, detstring.encode(), detweights, nlocal, nnz, timestamps, pixels, pixweights, signal, nperiod, periods, npsd, npsdtot, psdstarts, npsdbin, psdfreqs, npsdval, psdvals)

        return
