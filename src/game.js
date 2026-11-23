// BUILT (IRISH 2026) game rules: timing, HUB shifts, points, ranking points.
// Pure functions only (no Cloudflare APIs), tested by `npm test`.

// Match timing (manual 4.4)
export const AUTO_LEN = 20;
export const DELAY_LEN = 3;      // scoring delay between AUTO and TELEOP
export const TELEOP_LEN = 140;   // 2:20
export const MATCH_LENGTH = AUTO_LEN + DELAY_LEN + TELEOP_LEN; // 163 ticks
export const GRACE = 3;          // FUEL still counts for the old mapping 3s after a swap (4.5)

// TELEOP segments (Table 6-2 / 6-3): name, clock value the segment ends at,
// alliance credited for FUEL in the RED side of the HUB (blue side is the other one)
export const SEGMENTS = [
  ['TRANSITION', 130, 'blue'],
  ['SHIFT 1', 105, 'red'],
  ['SHIFT 2', 80, 'blue'],
  ['SHIFT 3', 55, 'red'],
  ['SHIFT 4', 30, 'blue'],
  ['END GAME', 0, 'red'],
];

// Point values (Table 6-4 / 6-6). Fouls are credited to the opponent.
export const POINTS = { auto_fuel: 4, teleop_fuel: 2, leave: 3, park: 2 };
export const FOUL_POINTS = { minor_foul: 2, major_foul: 6 };
export const COUNT_TYPES = [...Object.keys(POINTS), ...Object.keys(FOUL_POINTS)];

export const FUEL_BONUS_THRESHOLD = 80; // manual says TBD (60-100 FUEL); update when announced
export const AUTO_BONUS_FUEL = 5;
export const ROBOTS_PER_ALLIANCE = 2;   // ponytail: assumes no bypassed robots for AUTO BONUS
export const WIN_RP = 3;
export const TIE_RP = 1;

export const other = (alliance) => (alliance === 'red' ? 'blue' : 'red');

/** [period name, clock shown on screen, alliance the RED HUB side credits] */
export function matchState(elapsed) {
  if (elapsed < AUTO_LEN) return ['AUTO', AUTO_LEN - elapsed, 'red'];
  if (elapsed < AUTO_LEN + DELAY_LEN) return ['AUTO', 0, 'red'];
  const clock = Math.max(TELEOP_LEN - (elapsed - AUTO_LEN - DELAY_LEN), 0);
  for (const [name, end, redSide] of SEGMENTS) {
    if (clock > end) return [name, clock, redSide];
  }
  return ['END GAME', 0, 'red'];
}

/**
 * [alliance, event_type] for one FUEL exiting `hubSide` at `elapsed`.
 * Looks back GRACE seconds so FUEL right after a shift swap still counts for
 * the previous mapping. The AUTO->TELEOP delay already is AUTO's grace.
 */
export function creditFuel(hubSide, elapsed) {
  const lag = elapsed - GRACE;
  const [period, , redSide] = matchState(lag < AUTO_LEN + DELAY_LEN ? elapsed : lag);
  const alliance = hubSide === 'red' ? redSide : other(redSide);
  return [alliance, period === 'AUTO' ? 'auto_fuel' : 'teleop_fuel'];
}

/** Match points for `alliance` from the count fields on match row `m`. */
export function score(m, alliance) {
  const opp = other(alliance);
  let total = 0;
  for (const [t, p] of Object.entries(POINTS)) total += (m[`${alliance}_${t}`] || 0) * p;
  for (const [t, p] of Object.entries(FOUL_POINTS)) total += (m[`${opp}_${t}`] || 0) * p;
  return total;
}

/** [autoRp, fuelRp, connectionRp, total RP] for `alliance`. */
export function rankingPoints(m, alliance) {
  const auto = m[`${alliance}_leave`] >= ROBOTS_PER_ALLIANCE && m[`${alliance}_auto_fuel`] >= AUTO_BONUS_FUEL;
  const fuel = m[`${alliance}_auto_fuel`] + m[`${alliance}_teleop_fuel`] >= FUEL_BONUS_THRESHOLD;
  const conn = Boolean(m.connection);
  const mine = m[`${alliance}_score`];
  const theirs = m[`${other(alliance)}_score`];
  const result = mine > theirs ? WIN_RP : mine === theirs ? TIE_RP : 0;
  return [auto, fuel, conn, result + auto + fuel + conn];
}
