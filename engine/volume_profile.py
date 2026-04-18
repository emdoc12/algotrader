"""
Volume Profile and VWAP analysis from OHLCV candle data.

Pure computation module — no API calls, no caching.  Processes candle bars
that have already been fetched and exposes volume-weighted price levels,
support/resistance nodes, and volume statistics useful for trade decisions.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class VWAPData:
    """VWAP and associated bands / positioning."""
    vwap: float
    anchored_vwap: float          # from start of the bar window
    upper_band_1: float           # +1 std dev
    lower_band_1: float           # -1 std dev
    upper_band_2: float           # +2 std dev
    lower_band_2: float           # -2 std dev
    price_position: str           # "above" or "below"
    distance_pct: float           # distance from VWAP as a percentage


@dataclass
class VolumeNode:
    """A single price-level bucket in the volume profile."""
    price_low: float
    price_high: float
    price_mid: float
    volume: float
    node_type: str                # "HVN", "LVN", or "normal"


@dataclass
class VolumeProfileData:
    """Full volume profile result."""
    poc_price: float              # Point of Control price level
    poc_volume: float
    value_area_high: float        # upper bound of 70% value area
    value_area_low: float         # lower bound of 70% value area
    hvn: List[VolumeNode]         # High Volume Nodes
    lvn: List[VolumeNode]         # Low Volume Nodes
    nodes: List[VolumeNode]       # all buckets


@dataclass
class VolumeStats:
    """Aggregate volume statistics."""
    current_volume: float
    avg_volume_20: float          # 20-bar average
    volume_ratio: float           # current / average
    volume_trend: str             # "increasing" or "decreasing"
    buy_volume: float
    sell_volume: float
    buy_sell_ratio: float
    climax_detected: bool         # volume > 3x average


@dataclass
class VolumeAnalysis:
    """Top-level container returned by analyze()."""
    vwap: VWAPData
    profile: VolumeProfileData
    stats: VolumeStats


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class VolumeProfileAnalyzer:
    """Compute VWAP, volume profile, and volume statistics from OHLCV bars."""

    def __init__(self) -> None:
        # No state needed — pure computation.
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, bars: list, num_buckets: int = 50) -> VolumeAnalysis:
        """Run full volume analysis on a list of OHLCV bar objects.

        Each bar must expose attributes: timestamp, open, high, low, close, volume.

        Returns a ``VolumeAnalysis`` with VWAP data, volume profile, and stats.
        Handles edge cases (empty list, single bar, very narrow range).
        """
        if not bars:
            return self._empty_analysis()

        vwap_data = self._compute_vwap(bars)
        profile_data = self._compute_volume_profile(bars, num_buckets)
        stats = self._compute_volume_stats(bars)

        return VolumeAnalysis(vwap=vwap_data, profile=profile_data, stats=stats)

    def format_for_context(self, analysis: VolumeAnalysis, current_price: float) -> str:
        """Return a human-readable summary suitable for LLM context injection."""
        v = analysis.vwap
        p = analysis.profile
        s = analysis.stats

        lines: list[str] = []
        lines.append("=== Volume Profile Analysis ===")

        # --- VWAP ---
        direction = "above" if current_price >= v.vwap else "below"
        dist = abs(current_price - v.vwap) / v.vwap * 100 if v.vwap else 0.0
        lines.append(
            f"VWAP: ${v.vwap:,.2f} | Price is {direction} VWAP by {dist:.2f}%"
        )
        lines.append(
            f"  Bands: [{v.lower_band_2:,.2f}] [{v.lower_band_1:,.2f}] "
            f"-- VWAP -- [{v.upper_band_1:,.2f}] [{v.upper_band_2:,.2f}]"
        )

        # --- POC & Value Area ---
        lines.append(
            f"POC (Point of Control): ${p.poc_price:,.2f} "
            f"(volume {p.poc_volume:,.0f})"
        )
        lines.append(
            f"Value Area: ${p.value_area_low:,.2f} — ${p.value_area_high:,.2f}"
        )

        # --- Nearest HVN above / below ---
        hvn_above = [n for n in p.hvn if n.price_mid > current_price]
        hvn_below = [n for n in p.hvn if n.price_mid <= current_price]
        if hvn_below:
            nearest_below = max(hvn_below, key=lambda n: n.price_mid)
            lines.append(
                f"Nearest HVN support below: ${nearest_below.price_mid:,.2f} "
                f"(vol {nearest_below.volume:,.0f})"
            )
        else:
            lines.append("Nearest HVN support below: none in range")

        if hvn_above:
            nearest_above = min(hvn_above, key=lambda n: n.price_mid)
            lines.append(
                f"Nearest HVN resistance above: ${nearest_above.price_mid:,.2f} "
                f"(vol {nearest_above.volume:,.0f})"
            )
        else:
            lines.append("Nearest HVN resistance above: none in range")

        # --- Nearest LVN ---
        if p.lvn:
            nearest_lvn = min(p.lvn, key=lambda n: abs(n.price_mid - current_price))
            lvn_dir = "above" if nearest_lvn.price_mid > current_price else "below"
            lines.append(
                f"Nearest LVN ({lvn_dir}): ${nearest_lvn.price_mid:,.2f} "
                f"— price may move quickly through this zone"
            )
        else:
            lines.append("No significant LVN detected")

        # --- Volume Stats ---
        trend_emoji = "rising" if s.volume_trend == "increasing" else "falling"
        above_below = "above" if s.volume_ratio >= 1.0 else "below"
        lines.append(
            f"Volume trend: {trend_emoji} | "
            f"Current vol {above_below} average ({s.volume_ratio:.2f}x)"
        )
        lines.append(
            f"Buy/Sell ratio: {s.buy_sell_ratio:.2f} "
            f"(buy {s.buy_volume:,.0f} / sell {s.sell_volume:,.0f})"
        )

        if s.climax_detected:
            lines.append(
                "VOLUME CLIMAX DETECTED — volume > 3x average, "
                "potential exhaustion/reversal"
            )

        # --- Interpretation ---
        lines.append("")
        lines.append(self._interpret(analysis, current_price))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # VWAP computation
    # ------------------------------------------------------------------

    def _compute_vwap(self, bars: list) -> VWAPData:
        cum_tp_vol = 0.0
        cum_vol = 0.0
        cum_tp2_vol = 0.0  # for standard deviation bands

        for bar in bars:
            tp = (bar.high + bar.low + bar.close) / 3.0
            vol = bar.volume if bar.volume else 0.0
            cum_tp_vol += tp * vol
            cum_vol += vol
            cum_tp2_vol += (tp ** 2) * vol

        if cum_vol == 0:
            mid = (bars[-1].high + bars[-1].low + bars[-1].close) / 3.0
            return VWAPData(
                vwap=mid, anchored_vwap=mid,
                upper_band_1=mid, lower_band_1=mid,
                upper_band_2=mid, lower_band_2=mid,
                price_position="above" if bars[-1].close >= mid else "below",
                distance_pct=0.0,
            )

        vwap = cum_tp_vol / cum_vol

        # Variance = E[X^2] - E[X]^2
        variance = (cum_tp2_vol / cum_vol) - (vwap ** 2)
        std_dev = math.sqrt(max(variance, 0.0))

        # Anchored VWAP — same calculation over full window (already is anchored
        # to bar[0] by construction).
        anchored_vwap = vwap

        last_close = bars[-1].close
        distance_pct = abs(last_close - vwap) / vwap * 100 if vwap else 0.0

        return VWAPData(
            vwap=vwap,
            anchored_vwap=anchored_vwap,
            upper_band_1=vwap + std_dev,
            lower_band_1=vwap - std_dev,
            upper_band_2=vwap + 2 * std_dev,
            lower_band_2=vwap - 2 * std_dev,
            price_position="above" if last_close >= vwap else "below",
            distance_pct=distance_pct,
        )

    # ------------------------------------------------------------------
    # Volume Profile
    # ------------------------------------------------------------------

    def _compute_volume_profile(
        self, bars: list, num_buckets: int
    ) -> VolumeProfileData:
        price_low = min(bar.low for bar in bars)
        price_high = max(bar.high for bar in bars)

        # Guard against zero-range (single price across all bars).
        price_range = price_high - price_low
        if price_range == 0:
            price_low -= 0.5
            price_high += 0.5
            price_range = 1.0

        bucket_size = price_range / num_buckets

        # Initialise buckets.
        buckets: list[float] = [0.0] * num_buckets

        for bar in bars:
            # Distribute each bar's volume across the buckets its range touches.
            bar_low = bar.low
            bar_high = bar.high
            vol = bar.volume if bar.volume else 0.0
            if vol == 0:
                continue

            lo_idx = max(0, int((bar_low - price_low) / bucket_size))
            hi_idx = min(num_buckets - 1, int((bar_high - price_low) / bucket_size))

            # Spread volume evenly across touched buckets.
            span = hi_idx - lo_idx + 1
            vol_per_bucket = vol / span
            for i in range(lo_idx, hi_idx + 1):
                buckets[i] += vol_per_bucket

        # Build VolumeNode list.
        total_volume = sum(buckets)
        avg_volume = total_volume / num_buckets if num_buckets else 0.0

        nodes: list[VolumeNode] = []
        for i, vol in enumerate(buckets):
            bl = price_low + i * bucket_size
            bh = bl + bucket_size
            bm = (bl + bh) / 2.0
            if vol > avg_volume * 1.5:
                ntype = "HVN"
            elif vol < avg_volume * 0.5:
                ntype = "LVN"
            else:
                ntype = "normal"
            nodes.append(VolumeNode(
                price_low=bl, price_high=bh, price_mid=bm,
                volume=vol, node_type=ntype,
            ))

        # POC — bucket with highest volume.
        poc_node = max(nodes, key=lambda n: n.volume)

        # Value Area — 70% of total volume centred on POC.
        va_target = total_volume * 0.70
        va_low, va_high = self._compute_value_area(nodes, poc_node, va_target)

        hvn = [n for n in nodes if n.node_type == "HVN"]
        lvn = [n for n in nodes if n.node_type == "LVN"]

        return VolumeProfileData(
            poc_price=poc_node.price_mid,
            poc_volume=poc_node.volume,
            value_area_high=va_high,
            value_area_low=va_low,
            hvn=hvn,
            lvn=lvn,
            nodes=nodes,
        )

    @staticmethod
    def _compute_value_area(
        nodes: List[VolumeNode],
        poc_node: VolumeNode,
        target_volume: float,
    ) -> tuple:
        """Expand outward from POC until 70% of volume is captured."""
        poc_idx = nodes.index(poc_node)
        included = {poc_idx}
        accumulated = poc_node.volume

        lo = poc_idx
        hi = poc_idx

        while accumulated < target_volume:
            look_lo = lo - 1
            look_hi = hi + 1

            vol_lo = nodes[look_lo].volume if look_lo >= 0 else -1.0
            vol_hi = nodes[look_hi].volume if look_hi < len(nodes) else -1.0

            if vol_lo < 0 and vol_hi < 0:
                break  # expanded to full range

            if vol_lo >= vol_hi:
                lo = look_lo
                accumulated += vol_lo
                included.add(lo)
            else:
                hi = look_hi
                accumulated += vol_hi
                included.add(hi)

        va_low = nodes[lo].price_low
        va_high = nodes[hi].price_high
        return va_low, va_high

    # ------------------------------------------------------------------
    # Volume Stats
    # ------------------------------------------------------------------

    def _compute_volume_stats(self, bars: list) -> VolumeStats:
        volumes = [bar.volume if bar.volume else 0.0 for bar in bars]

        current_volume = volumes[-1] if volumes else 0.0

        # 20-period average (or however many bars are available).
        lookback = min(20, len(volumes))
        avg_20 = sum(volumes[-lookback:]) / lookback if lookback else 0.0

        volume_ratio = current_volume / avg_20 if avg_20 else 0.0

        # Volume trend over last 10 bars (simple linear direction).
        trend_window = min(10, len(volumes))
        if trend_window >= 2:
            first_half = volumes[-trend_window: -trend_window // 2]
            second_half = volumes[-trend_window // 2:]
            avg_first = sum(first_half) / len(first_half) if first_half else 0.0
            avg_second = sum(second_half) / len(second_half) if second_half else 0.0
            volume_trend = "increasing" if avg_second >= avg_first else "decreasing"
        else:
            volume_trend = "increasing"

        # Buy / sell volume estimate.
        buy_volume = 0.0
        sell_volume = 0.0
        for bar in bars:
            vol = bar.volume if bar.volume else 0.0
            if bar.close >= bar.open:
                buy_volume += vol
            else:
                sell_volume += vol

        buy_sell_ratio = buy_volume / sell_volume if sell_volume else (
            float("inf") if buy_volume else 1.0
        )

        climax_detected = current_volume > avg_20 * 3 if avg_20 else False

        return VolumeStats(
            current_volume=current_volume,
            avg_volume_20=avg_20,
            volume_ratio=volume_ratio,
            volume_trend=volume_trend,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            buy_sell_ratio=buy_sell_ratio,
            climax_detected=climax_detected,
        )

    # ------------------------------------------------------------------
    # Interpretation
    # ------------------------------------------------------------------

    def _interpret(self, analysis: VolumeAnalysis, current_price: float) -> str:
        v = analysis.vwap
        p = analysis.profile
        s = analysis.stats

        parts: list[str] = []

        # Price vs VWAP bias.
        if current_price >= v.vwap:
            bias = "bullish"
            parts.append(f"Price above VWAP (${v.vwap:,.2f})")
        else:
            bias = "bearish"
            parts.append(f"Price below VWAP (${v.vwap:,.2f})")

        # Volume trend.
        if s.volume_trend == "increasing":
            parts.append("with rising volume")
        else:
            parts.append("with declining volume")

        summary = " ".join(parts) + f" — {bias} bias"

        # POC reference.
        if current_price > p.poc_price:
            summary += f", watch POC at ${p.poc_price:,.2f} for support"
        else:
            summary += f", watch POC at ${p.poc_price:,.2f} for resistance"

        # Climax warning.
        if s.climax_detected:
            summary += " | VOLUME CLIMAX — possible exhaustion/reversal"

        # Value area note.
        if current_price < p.value_area_low:
            summary += f" | Price below value area (${p.value_area_low:,.2f}), oversold relative to volume"
        elif current_price > p.value_area_high:
            summary += f" | Price above value area (${p.value_area_high:,.2f}), overbought relative to volume"

        return f"Interpretation: {summary}"

    # ------------------------------------------------------------------
    # Edge-case: empty bars
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_analysis() -> VolumeAnalysis:
        zero_vwap = VWAPData(
            vwap=0.0, anchored_vwap=0.0,
            upper_band_1=0.0, lower_band_1=0.0,
            upper_band_2=0.0, lower_band_2=0.0,
            price_position="above", distance_pct=0.0,
        )
        zero_profile = VolumeProfileData(
            poc_price=0.0, poc_volume=0.0,
            value_area_high=0.0, value_area_low=0.0,
            hvn=[], lvn=[], nodes=[],
        )
        zero_stats = VolumeStats(
            current_volume=0.0, avg_volume_20=0.0, volume_ratio=0.0,
            volume_trend="increasing",
            buy_volume=0.0, sell_volume=0.0, buy_sell_ratio=1.0,
            climax_detected=False,
        )
        return VolumeAnalysis(vwap=zero_vwap, profile=zero_profile, stats=zero_stats)
