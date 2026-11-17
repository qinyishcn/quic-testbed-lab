"""Workload and phase constants shared between the testbed and its analysis."""

PROFILES = ("steady", "radio", "backhaul", "mixed", "mixed-clean", "mixed-feasible", "ack")
MODES = ("bbr", "outer", "radio", "dual")
REVISION_MODES = ("bbr", "radio", "bbr_radio")
REVISION_PROFILES = (*PROFILES[:-1], "forward-delay")
# Minimum and maximum Mbps, followed by the interval between samples in us.
TAIL_BACKHAUL = {
    "random-headroom": (14, 26, 40_000),
    "random-tight": (6, 18, 40_000),
    "random-middle": (8, 20, 40_000),
    "random-tight-fast": (6, 18, 20_000),
}
START = 1_100_000
COUNT = 64
DURATION = 3_850_000
SLOW = (1_650_000, 2_510_000)
REVERSE_STEP = (1_850_000, 2_640_000, 15_000)
FORWARD_STEP = (*SLOW, 15_000)
