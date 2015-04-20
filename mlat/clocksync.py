# -*- mode: python; indent-tabs-mode: nil -*-

import math
import time
import bisect
import logging
from contextlib import closing


class Clock(object):
    """A particular clock. Stores characteristics of a clock,
    and acts as part of the key in the clock pairing map.
    """

    def __init__(self, epoch, freq, max_freq_error, jitter):
        """Create a new clock representation.

        epoch: a string indicating a fixed epoch, or None if freerunning
        freq: the clock frequency in Hz (float)
        max_freq_error: the maximum expected relative frequency error (i.e. 1e-6 is 1PPM) (float)
        jitter: the expected jitter of a typical reading, in seconds, standard deviation  (float)
        """
        self.epoch = epoch
        self.freq = freq
        self.max_freq_error = max_freq_error
        self.jitter = jitter


def make_clock(clock_type):
    if clock_type == 'radarcape_gps':
        return Clock(epoch='gps_midnight', freq=1e9, max_freq_error=5e-6, jitter=50e-9)
    if clock_type == 'beast':
        return Clock(epoch=None, freq=12e6, max_freq_error=5e-6, jitter=100e-9)
    if clock_type == 'dump1090':
        return Clock(epoch=None, freq=12e6, max_freq_error=100e-6, jitter=500e-9)
    if clock_type == 'sbs3':
        return Clock(epoch=None, freq=20e6, max_freq_error=5e-6, jitter=100e-9)
    raise NotImplementedError


class ClockPairing(object):
    """Describes the current relative characteristics of a pair of clocks."""

    KP = 0.05
    KI = 0.01

    def __init__(self, base, peer):
        self.base = base
        self.peer = peer
        self.base_clock = base.clock
        self.peer_clock = peer.clock
        self.raw_drift = None
        self.drift = None
        self.i_drift = None
        self.n = 0
        self.ts_base = []
        self.ts_peer = []
        self.var = []
        self.var_sum = 0.0
        self.outliers = 0
        self.cumulative_error = 0.0

        self.relative_freq = peer.clock.freq / base.clock.freq
        self.i_relative_freq = base.clock.freq / peer.clock.freq
        self.drift_max = base.clock.max_freq_error + peer.clock.max_freq_error
        self.drift_max_delta = self.drift_max / 10.0
        self.outlier_threshold = 5 * (peer.clock.jitter + base.clock.jitter)   # 5 sigma

        now = time.monotonic()
        self.expiry = now + 120.0
        self.validity = now + 30.0

    def is_new(self, base_ts):
        """Returns True if the given base timestamp is in the extrapolation region."""
        return bool(self.n == 0 or self.ts_base[-1] < base_ts)

    @property
    def variance(self):
        """Variance of recent predictions of the sync point versus the actual sync point."""
        if self.n == 0:
            return None
        return self.var_sum / self.n

    @property
    def error(self):
        """Standard error of recent predictions."""
        if self.n == 0:
            return None
        return math.sqrt(self.var_sum / self.n)

    @property
    def valid(self):
        """True if this pairing is usable for clock syncronization."""
        return bool(self.n >= 2 and self.variance < 2500 and self.outliers == 0 and self.validity > time.monotonic())

    def update(self, address, base_ts, peer_ts, base_interval, peer_interval):
        """Update the relative drift and offset of this pairing given:

        address: the ICAO address of the sync aircraft, for logging purposes
        base_ts: the timestamp of a recent point in time measured by the base clock
        peer_ts: the timestamp of the same point in time measured by the peer clock
        base_interval: the duration of a recent interval measured by the base clock
        peer_interval: the duration of the same interval measured by the peer clock

        Returns True if the update was used, False if it was an outlier.
        """

        # clean old data
        self._prune_old_data(base_ts)

        # predict from existing data, compare to actual value
        if self.n > 0:
            prediction = self.predict_peer(base_ts)
            prediction_error = (prediction - peer_ts) / self.peer_clock.freq

            if abs(prediction_error) > self.outlier_threshold and abs(prediction_error) > self.error * 5:
                self.outliers += 1
                if self.outliers < 5:
                    # don't accept this one
                    return False
        else:
            prediction_error = 0  # first sync point, no error

        # update clock drift based on interval ratio
        # this might reject the update
        if not self._update_drift(address, base_interval, peer_interval):
            return False

        # update clock offset based on the actual clock values
        self._update_offset(address, base_ts, peer_ts, prediction_error)

        now = time.monotonic()
        self.expiry = now + 120.0
        self.validity = now + 30.0
        return True

    def _prune_old_data(self, latest_base_ts):
        i = 0
        while i < self.n and (latest_base_ts - self.ts_base[i]) > 30*self.base_clock.freq:
            i += 1

        if i > 0:
            del self.ts_base[0:i]
            del self.ts_peer[0:i]
            del self.var[0:i]
            self.n -= i
            self.var_sum = sum(self.var)

    def _update_drift(self, address, base_interval, peer_interval):
        # try to reduce the effects of catastropic cancellation here:
        #new_drift = (peer_interval / base_interval) / self.relative_freq - 1.0
        adjusted_base_interval = base_interval * self.relative_freq
        new_drift = (peer_interval - adjusted_base_interval) / adjusted_base_interval

        if abs(new_drift) > self.drift_max:
            # Bad data, ignore entirely
            return False

        if self.drift is None:
            # First sample, just trust it outright
            self.raw_drift = self.drift = new_drift
            self.i_drift = -self.drift / (1.0 + self.drift)
            return True

        drift_error = new_drift - self.raw_drift
        if abs(drift_error) > self.drift_max_delta:
            # Too far away from the value we expect, discard
            return False

        # move towards the new value
        self.raw_drift += drift_error * self.KP
        self.drift = self.raw_drift - self.KI * self.cumulative_error
        self.i_drift = -self.drift / (1.0 + self.drift)
        return True

    def _update_offset(self, address, base_ts, peer_ts, prediction_error):
        # insert this into self.ts_base / self.ts_peer / self.var in the right place
        if self.n == 0:
            i = 0
        else:
            i = bisect.bisect_left(self.ts_base, base_ts)

            # ts_base and ts_peer define a function constructed by linearly
            # interpolating between each pair of values.
            #
            # This function must be monotonically increasing or one of our clocks
            # has effectively gone backwards. If this happens, give up and start
            # again.
            if (((i < self.n and self.ts_peer[i] < peer_ts) or
                 (i > 0 and self.ts_peer[i-1] > peer_ts))):
                logging.info("{0}: monotonicity broken, reset".format(self))
                self.ts_base = []
                self.ts_peer = []
                self.var = []
                self.var_sum = 0
                self.cumulative_error = 0
                self.n = 0
                i = 0

            if i < self.n:
                logging.info("{0}: not at the end when adding a new offset".format(self))

        self.n += 1
        self.ts_base.insert(i, base_ts)
        self.ts_peer.insert(i, peer_ts)

        p_var = prediction_error ** 2
        self.var.insert(i, p_var)
        self.var_sum += p_var

        # if we are accepting an outlier, do not include it in our integral term
        if not self.outliers:
            self.cumulative_error = max(-50e-6, min(50e-6, prediction_error))  # limit to 50us

        with closing(open('clocks.csv', 'a')) as f:
            line = '{t:.3f},{base},{peer},{drift:.2f},{rdrift:.2f},{err:.2f},{cerr:.2f},{stddev:.2f},{o}'.format(
                t=time.time(),
                base=self.base.user,
                peer=self.peer.user,
                drift=self.drift*1e6,
                rdrift=self.raw_drift*1e6,
                err=prediction_error*1e6,
                cerr=self.cumulative_error*1e6,
                stddev=self.error*1e6,
                o=self.outliers)
            print(line, file=f)

        self.outliers = max(0, self.outliers - 2)

        if abs(prediction_error) > self.outlier_threshold:
            logging.info("{r}: {a:06X}: step by {e:.1f}us".format(r=self, a=address, e=prediction_error*1e6))

    def predict_peer(self, base_ts):
        if self.n == 0:
            return None

        i = bisect.bisect_left(self.ts_base, base_ts)
        if i == 0:
            # extrapolate before first point
            elapsed = base_ts - self.ts_base[0]
            return (self.ts_peer[0] +
                    elapsed * self.relative_freq +
                    elapsed * self.relative_freq * self.drift)
        elif i == self.n:
            # extrapolate after last point
            elapsed = base_ts - self.ts_base[-1]
            return (self.ts_peer[-1] +
                    elapsed * self.relative_freq +
                    elapsed * self.relative_freq * self.drift)
        else:
            # interpolate between two points
            return (self.ts_peer[i-1] +
                    (self.ts_peer[i] - self.ts_peer[i-1]) *
                    (base_ts - self.ts_base[i-1]) /
                    (self.ts_base[i] - self.ts_base[i-1]))

    def predict_base(self, peer_ts):
        if self.n == 0:
            return None

        i = bisect.bisect_left(self.ts_peer, peer_ts)
        if i == 0:
            # extrapolate before first point
            elapsed = peer_ts - self.ts_peer[0]
            return (self.ts_base[0] +
                    elapsed * self.i_relative_freq +
                    elapsed * self.i_relative_freq * self.i_drift)
        elif i == self.n:
            # extrapolate after last point
            elapsed = peer_ts - self.ts_peer[-1]
            return (elapsed +
                    elapsed * self.i_relative_freq +
                    elapsed * self.i_relative_freq * self.i_drift)
        else:
            # interpolate between two points
            return (self.ts_base[i-1] +
                    (self.ts_base[i] - self.ts_base[i-1]) *
                    (peer_ts - self.ts_peer[i-1]) /
                    (self.ts_peer[i] - self.ts_peer[i-1]))

    def __str__(self):
        return self.base.user + ':' + self.peer.user