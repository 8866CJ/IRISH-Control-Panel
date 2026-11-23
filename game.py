"""BUILT (IRISH 2026) game rules: timing, HUB shifts, points, ranking points.

Pure functions only, no Flask/DB, so `python game.py` runs the self-check.
"""

# Match timing (manual 4.4)
AUTO_LEN = 20
DELAY_LEN = 3          # scoring delay between AUTO and TELEOP
TELEOP_LEN = 140       # 2:20
MATCH_LENGTH = AUTO_LEN + DELAY_LEN + TELEOP_LEN  # 163 ticks
GRACE = 3              # FUEL still counts for the old mapping 3s after a swap (4.5)

# TELEOP segments (Table 6-2 / 6-3): name, clock value the segment ends at,
# alliance credited for FUEL in the RED side of the HUB (blue side is the other one)
SEGMENTS = [
    ('TRANSITION', 130, 'blue'),
    ('SHIFT 1', 105, 'red'),
    ('SHIFT 2', 80, 'blue'),
    ('SHIFT 3', 55, 'red'),
    ('SHIFT 4', 30, 'blue'),
    ('END GAME', 0, 'red'),
]

# Point values (Table 6-4 / 6-6). Fouls are credited to the opponent.
POINTS = {'auto_fuel': 4, 'teleop_fuel': 2, 'leave': 3, 'park': 2}
FOUL_POINTS = {'minor_foul': 2, 'major_foul': 6}
COUNT_TYPES = list(POINTS) + list(FOUL_POINTS)

FUEL_BONUS_THRESHOLD = 80   # manual says TBD (60-100 FUEL); update when announced
AUTO_BONUS_FUEL = 5
ROBOTS_PER_ALLIANCE = 2     # ponytail: assumes no bypassed robots for AUTO BONUS
WIN_RP, TIE_RP = 3, 1


def other(alliance):
    return 'blue' if alliance == 'red' else 'red'


def match_state(elapsed):
    """(period name, clock shown on screen, alliance the RED HUB side credits)."""
    if elapsed < AUTO_LEN:
        return 'AUTO', AUTO_LEN - elapsed, 'red'
    if elapsed < AUTO_LEN + DELAY_LEN:
        return 'AUTO', 0, 'red'
    clock = max(TELEOP_LEN - (elapsed - AUTO_LEN - DELAY_LEN), 0)
    for name, end, red_side in SEGMENTS:
        if clock > end:
            return name, clock, red_side
    return 'END GAME', 0, 'red'


def credit_fuel(hub_side, elapsed):
    """(alliance, event_type) for one FUEL exiting `hub_side` at `elapsed`.

    Looks back GRACE seconds so FUEL right after a shift swap still counts for
    the previous mapping. The AUTO->TELEOP delay already is AUTO's grace.
    """
    lag = elapsed - GRACE
    period, _, red_side = match_state(elapsed if lag < AUTO_LEN + DELAY_LEN else lag)
    alliance = red_side if hub_side == 'red' else other(red_side)
    return alliance, 'auto_fuel' if period == 'AUTO' else 'teleop_fuel'


def score(m, alliance):
    """Match points for `alliance` from the count fields on match-like object `m`."""
    opp = other(alliance)
    return (sum(getattr(m, f'{alliance}_{t}') * p for t, p in POINTS.items())
            + sum(getattr(m, f'{opp}_{t}') * p for t, p in FOUL_POINTS.items()))


def ranking_points(m, alliance):
    """(auto_rp, fuel_rp, connection_rp, total RP) for `alliance`."""
    auto = (getattr(m, f'{alliance}_leave') >= ROBOTS_PER_ALLIANCE
            and getattr(m, f'{alliance}_auto_fuel') >= AUTO_BONUS_FUEL)
    fuel = (getattr(m, f'{alliance}_auto_fuel') + getattr(m, f'{alliance}_teleop_fuel')
            >= FUEL_BONUS_THRESHOLD)
    conn = bool(m.connection)
    mine, theirs = getattr(m, f'{alliance}_score'), getattr(m, f'{other(alliance)}_score')
    result = WIN_RP if mine > theirs else TIE_RP if mine == theirs else 0
    return auto, fuel, conn, result + auto + fuel + conn


if __name__ == '__main__':
    from types import SimpleNamespace

    # Timing / shift table
    assert match_state(0) == ('AUTO', 20, 'red')
    assert match_state(21) == ('AUTO', 0, 'red')
    assert match_state(23) == ('TRANSITION', 140, 'blue')
    assert match_state(33) == ('SHIFT 1', 130, 'red')
    assert match_state(58) == ('SHIFT 2', 105, 'blue')
    assert match_state(133) == ('END GAME', 30, 'red')
    assert match_state(MATCH_LENGTH) == ('END GAME', 0, 'red')

    # FUEL credit: AUTO, delay, TELEOP start, grace after a swap
    assert credit_fuel('red', 5) == ('red', 'auto_fuel')
    assert credit_fuel('red', 22) == ('red', 'auto_fuel')       # 3s delay still AUTO
    assert credit_fuel('red', 24) == ('blue', 'teleop_fuel')    # TRANSITION: swapped
    assert credit_fuel('red', 34) == ('blue', 'teleop_fuel')    # 1s into SHIFT 1: grace
    assert credit_fuel('red', 36) == ('red', 'teleop_fuel')     # grace over
    assert credit_fuel('blue', 36) == ('blue', 'teleop_fuel')
    assert credit_fuel('red', MATCH_LENGTH) == ('red', 'teleop_fuel')

    # Score + RP
    m = SimpleNamespace(connection=True, **{f'{a}_{t}': 0 for a in ('red', 'blue') for t in COUNT_TYPES})
    m.red_auto_fuel, m.red_teleop_fuel, m.red_leave, m.red_park = 5, 75, 2, 1
    m.blue_minor_foul, m.blue_major_foul, m.red_major_foul = 1, 1, 1
    m.red_score, m.blue_score = score(m, 'red'), score(m, 'blue')
    assert m.red_score == 20 + 150 + 6 + 2 + 2 + 6
    assert m.blue_score == 6
    assert ranking_points(m, 'red') == (True, True, True, 6)
    assert ranking_points(m, 'blue') == (False, False, True, 1)
    m.blue_score = m.red_score
    assert ranking_points(m, 'blue')[3] == 2  # tie + connection
    print('game.py self-check OK')
