import test from 'node:test';
import assert from 'node:assert/strict';
import { matchState, creditFuel, score, rankingPoints, MATCH_LENGTH, COUNT_TYPES } from '../src/game.js';

test('timing and shift table', () => {
  assert.deepEqual(matchState(0), ['AUTO', 20, 'red']);
  assert.deepEqual(matchState(21), ['AUTO', 0, 'red']);
  assert.deepEqual(matchState(23), ['TRANSITION', 140, 'blue']);
  assert.deepEqual(matchState(33), ['SHIFT 1', 130, 'red']);
  assert.deepEqual(matchState(58), ['SHIFT 2', 105, 'blue']);
  assert.deepEqual(matchState(133), ['END GAME', 30, 'red']);
  assert.deepEqual(matchState(MATCH_LENGTH), ['END GAME', 0, 'red']);
});

test('FUEL credit: AUTO, delay, TELEOP start, grace after a swap', () => {
  assert.deepEqual(creditFuel('red', 5), ['red', 'auto_fuel']);
  assert.deepEqual(creditFuel('red', 22), ['red', 'auto_fuel']);    // 3s delay still AUTO
  assert.deepEqual(creditFuel('red', 24), ['blue', 'teleop_fuel']); // TRANSITION: swapped
  assert.deepEqual(creditFuel('red', 34), ['blue', 'teleop_fuel']); // 1s into SHIFT 1: grace
  assert.deepEqual(creditFuel('red', 36), ['red', 'teleop_fuel']);  // grace over
  assert.deepEqual(creditFuel('blue', 36), ['blue', 'teleop_fuel']);
  assert.deepEqual(creditFuel('red', MATCH_LENGTH), ['red', 'teleop_fuel']);
});

test('score and ranking points', () => {
  const m = { connection: 1 };
  for (const a of ['red', 'blue']) for (const t of COUNT_TYPES) m[`${a}_${t}`] = 0;
  Object.assign(m, { red_auto_fuel: 5, red_teleop_fuel: 75, red_leave: 2, red_park: 1, blue_minor_foul: 1, blue_major_foul: 1, red_major_foul: 1 });
  m.red_score = score(m, 'red');
  m.blue_score = score(m, 'blue');
  assert.equal(m.red_score, 20 + 150 + 6 + 2 + 2 + 6);
  assert.equal(m.blue_score, 6);
  assert.deepEqual(rankingPoints(m, 'red'), [true, true, true, 6]);
  assert.deepEqual(rankingPoints(m, 'blue'), [false, false, true, 1]);
  m.blue_score = m.red_score;
  assert.equal(rankingPoints(m, 'blue')[3], 2); // tie + connection
});
