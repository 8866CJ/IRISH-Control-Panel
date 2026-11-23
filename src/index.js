// IRISH control panel on Cloudflare.
// Static pages come from ./public (Workers static assets). Everything live — the
// /api routes, the /ws WebSocket and the match clock — runs in ONE Durable Object
// ("Field") so every scoring tap, timer tick and broadcast is handled in order.
import { DurableObject } from 'cloudflare:workers';
import * as game from './game.js';

export default {
  // Only /api/* and /ws reach this (see assets.run_worker_first in wrangler.jsonc).
  fetch(request, env) {
    return env.FIELD.get(env.FIELD.idFromName('irish')).fetch(request);
  },
};

const TEAM_KEYS = ['red_team1', 'red_team2', 'blue_team1', 'blue_team2'];
const MATCH_SELECT = `SELECT m.*,
  r1.number AS red_team1_number, r2.number AS red_team2_number,
  b1.number AS blue_team1_number, b2.number AS blue_team2_number
  FROM match m
  LEFT JOIN team r1 ON r1.id = m.red_team1_id LEFT JOIN team r2 ON r2.id = m.red_team2_id
  LEFT JOIN team b1 ON b1.id = m.blue_team1_id LEFT JOIN team b2 ON b2.id = m.blue_team2_id`;

const json = (data, status = 200) => Response.json(data, { status });
const now = () => new Date().toISOString();
const toInt = (v) => (/^\d+$/.test(String(v ?? '').trim()) ? Number(String(v).trim()) : null);

export class Field extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.db = env.DB;
    this.authFails = { count: 0, since: 0 };
  }

  // ---------- helpers ----------

  q(sql, ...args) { return this.db.prepare(sql).bind(...args); }
  all(sql, ...args) { return this.q(sql, ...args).all().then((r) => r.results); }
  first(sql, ...args) { return this.q(sql, ...args).first(); }
  run(sql, ...args) { return this.q(sql, ...args).run(); }

  broadcast(event, data = {}) {
    const msg = JSON.stringify({ event, data });
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.send(msg); } catch { /* socket already gone */ }
    }
  }

  send(ws, event, data = {}) {
    try { ws.send(JSON.stringify({ event, data })); } catch { /* gone */ }
  }

  async state() {
    return (await this.ctx.storage.get('clock')) || { matchId: null, running: false, fault: false, nextTick: 0, clearEndgame: false };
  }
  setState(s) { return this.ctx.storage.put('clock', s); }

  /** What the audience display should be showing, so a reloaded display can restore it. */
  async setScreen(patch) {
    const cur = (await this.ctx.storage.get('screen')) || {};
    await this.ctx.storage.put('screen', { ...cur, ...patch });
  }

  /** Event PIN check with a crude lockout: 20 wrong tries a minute, then everyone waits. */
  checkPin(pin) {
    const expected = this.env.EVENT_PIN;
    if (!expected) return false; // fail closed if the secret was never set
    const t = Date.now();
    if (t - this.authFails.since > 60_000) this.authFails = { count: 0, since: t };
    // ponytail: in-memory lockout, resets if the object restarts; fine for one event
    if (this.authFails.count >= 20) return false;
    const ok = String(pin ?? '') === expected;
    if (!ok) this.authFails.count++;
    return ok;
  }

  async getMatch(id) {
    if (id == null) return null;
    return this.first(`${MATCH_SELECT} WHERE m.id = ?`, id);
  }

  matchDict(m) {
    const d = { ...m };
    for (const k of TEAM_KEYS) { d[k] = m[`${k}_number`] ?? ''; delete d[`${k}_number`]; }
    delete d.start_time; delete d.end_time;
    d.match_id = m.id;
    d.red_win_rp = m.red_score > m.blue_score;
    d.blue_win_rp = m.blue_score > m.red_score;
    return d;
  }

  timerPayload(m, fault) {
    const [period, clock, redSide] = game.matchState(game.MATCH_LENGTH - m.match_time_remaining);
    const segment = game.SEGMENTS.find(([name]) => name === period);
    return {
      match_id: m.id,
      time_remaining: clock,
      period,
      is_endgame: period === 'END GAME',
      next_swap_in: segment && period !== 'END GAME' ? clock - segment[1] : null, // seconds until the HUB sides swap
      red_side_alliance: redSide, // which alliance FUEL in the RED side of the HUB counts for
      field_fault: fault,
    };
  }

  async nextMatchNumber() {
    const r = await this.first('SELECT MAX(match_number) AS n FROM match');
    return (r?.n || 0) + 1;
  }

  /** Find a team by number, creating "Team N" if it doesn't exist. Returns its id. */
  async teamId(number) {
    await this.run("INSERT INTO team (number, name) VALUES (?, 'Team ' || ?) ON CONFLICT(number) DO NOTHING", number, number);
    return (await this.first('SELECT id FROM team WHERE number = ?', number)).id;
  }

  /** Rebuild every count and both scores from the match's event log. */
  async recount(matchId) {
    const m = { connection: 0 };
    for (const a of ['red', 'blue']) for (const t of game.COUNT_TYPES) m[`${a}_${t}`] = 0;
    const rows = await this.all('SELECT alliance, event_type, COUNT(*) AS c FROM match_event WHERE match_id = ? GROUP BY alliance, event_type', matchId);
    for (const r of rows) {
      if (r.event_type === 'connection') m.connection = 1;
      else if (game.COUNT_TYPES.includes(r.event_type) && (r.alliance === 'red' || r.alliance === 'blue')) m[`${r.alliance}_${r.event_type}`] = r.c;
    }
    m.red_score = game.score(m, 'red');
    m.blue_score = game.score(m, 'blue');
    const cols = Object.keys(m);
    await this.run(`UPDATE match SET ${cols.map((c) => `${c} = ?`).join(', ')} WHERE id = ?`, ...cols.map((c) => m[c]), matchId);
  }

  /** Set RP flags and update team rankings. Safe to call repeatedly: only the change since the last call is added. */
  async calculateRps(matchId) {
    const m = await this.getMatch(matchId);
    if (!m) return;
    const countsForRanking = (m.match_type || '').toLowerCase() === 'qualification';
    const stmts = [];
    for (const a of ['red', 'blue']) {
      const [auto, fuel, , rp] = game.rankingPoints(m, a);
      const total = countsForRanking ? rp : 0;
      const delta = total - (m[`${a}_rp`] || 0);
      stmts.push(this.q(`UPDATE match SET ${a}_auto_rp = ?, ${a}_fuel_rp = ?, ${a}_rp = ? WHERE id = ?`, +auto, +fuel, total, matchId));
      if (delta) {
        for (const id of [m[`${a}_team1_id`], m[`${a}_team2_id`]]) {
          if (id) stmts.push(this.q('INSERT INTO team_ranking (team_id, ranking_points) VALUES (?, ?) ON CONFLICT(team_id) DO UPDATE SET ranking_points = ranking_points + excluded.ranking_points', id, delta));
        }
      }
    }
    await this.db.batch(stmts);
    await this.updateRankings();
  }

  /** Recalculate rank positions, keeping the previous rank for the arrows. */
  async updateRankings() {
    const rows = await this.all('SELECT id, current_rank FROM team_ranking ORDER BY ranking_points DESC, id');
    if (rows.length) {
      await this.db.batch(rows.map((r, i) => this.q('UPDATE team_ranking SET previous_rank = ?, current_rank = ? WHERE id = ?', r.current_rank, i + 1, r.id)));
    }
    this.broadcast('rankings_updated');
  }

  rankChange(r) {
    if (r.previous_rank == null) return '—';
    if (r.current_rank < r.previous_rank) return '↑';
    if (r.current_rank > r.previous_rank) return '↓';
    return '—';
  }

  // ---------- HTTP ----------

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === '/ws') {
      if (request.headers.get('Upgrade') !== 'websocket') return new Response('Expected WebSocket', { status: 426 });
      const [client, server] = Object.values(new WebSocketPair());
      this.ctx.acceptWebSocket(server);
      server.serializeAttachment({ authed: false });
      return new Response(null, { status: 101, webSocket: client });
    }
    try {
      return await this.api(request, url);
    } catch (err) {
      console.error(err);
      return json({ error: 'Server error' }, 500);
    }
  }

  async api(request, url) {
    const method = request.method;
    const path = url.pathname;
    const isRead = method === 'GET' && !path.startsWith('/api/admin/');
    if (!isRead && !this.checkPin(request.headers.get('X-Event-PIN'))) return json({ error: 'Event PIN required' }, 401);
    const body = method === 'POST' || method === 'PUT' ? await request.json().catch(() => ({})) : {};
    let m;

    if (path === '/api/current_match' && method === 'GET') {
      const match = (await this.first(`${MATCH_SELECT} WHERE m.status = 'in_progress' LIMIT 1`))
        || (await this.first(`${MATCH_SELECT} WHERE m.status = 'scheduled' LIMIT 1`))
        || (await this.first(`${MATCH_SELECT} ORDER BY m.id DESC LIMIT 1`));
      if (match) return json(this.matchDict(match));
      return json({ match_number: 1, match_type: 'Qualification', red_team1: '', red_team2: '', blue_team1: '', blue_team2: '', red_score: 0, blue_score: 0, status: 'scheduled' });
    }

    if (path === '/api/matches' && method === 'GET') {
      const rows = await this.all(`${MATCH_SELECT} ORDER BY m.id`);
      return json(rows.map((r) => {
        const d = this.matchDict(r);
        for (const k of TEAM_KEYS) d[k] = d[k] === '' ? null : { number: d[k] }; // FTA/admin expect {number} or null
        return d;
      }));
    }

    if ((m = path.match(/^\/api\/matches\/(\d+)$/)) && method === 'DELETE') {
      const id = Number(m[1]);
      if (!(await this.getMatch(id))) return json({ error: 'Match not found' }, 404);
      await this.db.batch([this.q('DELETE FROM match_event WHERE match_id = ?', id), this.q('DELETE FROM match WHERE id = ?', id)]);
      return json({ success: true, message: `Match ${id} deleted` });
    }

    if ((m = path.match(/^\/api\/match_events\/(\d+)$/)) && method === 'GET') {
      return json(await this.all('SELECT id, match_id, event_type, alliance, points, timestamp, details FROM match_event WHERE match_id = ? ORDER BY timestamp, id', Number(m[1])));
    }

    if (path === '/api/rankings' && method === 'GET') {
      const rows = await this.all('SELECT r.*, t.number AS team_number, t.name AS team_name FROM team_ranking r JOIN team t ON t.id = r.team_id ORDER BY r.ranking_points DESC, r.id');
      return json(rows.map((r) => ({ team_number: r.team_number, team_name: r.team_name, ranking_points: r.ranking_points, rank: r.current_rank, rank_change: this.rankChange(r) })));
    }

    if ((m = path.match(/^\/api\/team_rank\/(\d+)$/)) && method === 'GET') {
      const number = Number(m[1]);
      const team = await this.first('SELECT id FROM team WHERE number = ?', number);
      if (!team) return json({ error: 'Team not found' }, 404);
      const r = await this.first('SELECT * FROM team_ranking WHERE team_id = ?', team.id);
      if (!r) return json({ team_number: number, rank: null, ranking_points: 0, rank_change: '—' });
      return json({ team_number: number, rank: r.current_rank, ranking_points: r.ranking_points, rank_change: this.rankChange(r) });
    }

    if (path === '/api/next_match_number' && method === 'GET') {
      return json({ next_match_number: await this.nextMatchNumber() });
    }

    if (path === '/api/teams' && method === 'GET') {
      return json(await this.all('SELECT id, number, name FROM team ORDER BY number'));
    }

    if (path === '/api/teams' && method === 'POST') {
      const number = toInt(body.number);
      const name = String(body.name ?? '').trim();
      if (!number || !name) return json({ error: 'Team number and name are required' }, 400);
      if (await this.first('SELECT id FROM team WHERE number = ?', number)) return json({ error: `Team ${number} already exists` }, 400);
      const { meta } = await this.run('INSERT INTO team (number, name) VALUES (?, ?)', number, name);
      return json({ id: meta.last_row_id, number, name, message: 'Team added successfully' }, 201);
    }

    if ((m = path.match(/^\/api\/teams\/(\d+)$/)) && (method === 'PUT' || method === 'DELETE')) {
      const id = Number(m[1]);
      const team = await this.first('SELECT * FROM team WHERE id = ?', id);
      if (!team) return json({ error: 'Team not found' }, 404);
      if (method === 'PUT') {
        const name = String(body.name ?? '').trim();
        if (!name) return json({ error: 'Team name is required' }, 400);
        await this.run('UPDATE team SET name = ? WHERE id = ?', name, id);
        return json({ id, number: team.number, name, message: 'Team updated successfully' });
      }
      const { n } = await this.first('SELECT COUNT(*) AS n FROM match WHERE ? IN (red_team1_id, red_team2_id, blue_team1_id, blue_team2_id)', id);
      if (n > 0) return json({ error: `Cannot delete team. Team has ${n} associated match(es). Delete matches first.` }, 400);
      await this.db.batch([this.q('DELETE FROM team_ranking WHERE team_id = ?', id), this.q('DELETE FROM team WHERE id = ?', id)]);
      return json({ message: 'Team deleted successfully' });
    }

    // ----- admin -----

    if (path === '/api/admin/matches/all' && method === 'DELETE') {
      await this.db.batch([this.q('DELETE FROM match_event'), this.q('DELETE FROM match')]);
      return json({ success: true, message: 'All matches and events deleted' });
    }

    if ((m = path.match(/^\/api\/admin\/matches\/(\d+)$/)) && method === 'PUT') {
      const id = Number(m[1]);
      if (!(await this.getMatch(id))) return json({ error: 'Match not found' }, 404);
      const sets = [];
      const args = [];
      for (const k of ['match_number', 'red_score', 'blue_score']) if (k in body && toInt(body[k]) != null) { sets.push(`${k} = ?`); args.push(toInt(body[k])); }
      for (const k of ['match_type', 'status']) if (k in body) { sets.push(`${k} = ?`); args.push(String(body[k])); }
      for (const k of TEAM_KEYS) {
        if (body[k]) {
          const team = await this.first('SELECT id FROM team WHERE number = ?', toInt(body[k]));
          sets.push(`${k}_id = ?`); args.push(team ? team.id : null);
        }
      }
      if (sets.length) await this.run(`UPDATE match SET ${sets.join(', ')} WHERE id = ?`, ...args, id);
      return json({ success: true, message: 'Match updated successfully', match_id: id });
    }

    if ((m = path.match(/^\/api\/admin\/rankings\/(\d+)$/)) && method === 'PUT') {
      const number = Number(m[1]);
      const team = await this.first('SELECT id FROM team WHERE number = ?', number);
      if (!team) return json({ error: 'Team not found' }, 404);
      if (!(await this.first('SELECT id FROM team_ranking WHERE team_id = ?', team.id))) return json({ error: 'Ranking not found' }, 404);
      const rp = Number(body.ranking_points);
      if (!Number.isFinite(rp)) return json({ error: 'ranking_points is required' }, 400);
      await this.run('UPDATE team_ranking SET ranking_points = ? WHERE team_id = ?', rp, team.id);
      await this.updateRankings();
      return json({ success: true, message: 'Ranking updated successfully', team_number: String(number), ranking_points: rp });
    }

    if (path === '/api/admin/rankings/reset' && method === 'POST') {
      await this.run('UPDATE team_ranking SET ranking_points = 0, previous_rank = 0, current_rank = 0');
      await this.updateRankings();
      return json({ success: true, message: 'All rankings reset to 0' });
    }

    if (path === '/api/admin/events' && method === 'GET') {
      const matchId = toInt(url.searchParams.get('match_id'));
      const rows = matchId
        ? await this.all('SELECT id, match_id, alliance, event_type, points, timestamp FROM match_event WHERE match_id = ? ORDER BY timestamp DESC, id DESC', matchId)
        : await this.all('SELECT id, match_id, alliance, event_type, points, timestamp FROM match_event ORDER BY timestamp DESC, id DESC');
      return json(rows);
    }

    if ((m = path.match(/^\/api\/admin\/events\/(\d+)$/)) && method === 'DELETE') {
      const ev = await this.first('SELECT * FROM match_event WHERE id = ?', Number(m[1]));
      if (!ev) return json({ error: 'Event not found' }, 404);
      await this.run('DELETE FROM match_event WHERE id = ?', ev.id);
      await this.recount(ev.match_id); // keep the score in sync with the event log
      return json({ success: true, message: 'Event deleted successfully' });
    }

    return json({ error: 'Not found' }, 404);
  }

  // ---------- WebSocket ----------

  async webSocketMessage(ws, raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    const event = msg?.event;
    const data = msg?.data && typeof msg.data === 'object' ? msg.data : {};

    if (event === 'auth') {
      const ok = this.checkPin(data.pin);
      ws.serializeAttachment({ authed: ok });
      return this.send(ws, 'auth_result', { ok });
    }
    if (event === 'sync') return this.sync(ws);

    const handler = HANDLERS[event];
    if (!handler) return;
    if (!ws.deserializeAttachment()?.authed) return this.send(ws, 'auth_error', { event });
    try {
      await handler.call(this, data, ws);
    } catch (err) {
      console.error(event, err);
    }
  }

  webSocketClose(ws, code) {
    try { ws.close(code); } catch { /* already closed */ }
  }

  /** A page that (re)connects catches up: displays get the current screen, phones the running match. */
  async sync(ws) {
    const s = await this.state();
    const screen = await this.ctx.storage.get('screen');
    if (screen?.name) {
      const out = { screen: screen.name, review: Boolean(screen.review), data: screen.prematch || null, timer: null };
      const sm = screen.name === 'live' || screen.name === 'postmatch' ? await this.getMatch(screen.matchId) : null;
      if (sm) {
        out.data = this.matchDict(sm);
        if (screen.name === 'live') {
          const running = s.running && s.matchId === sm.id;
          out.timer = running ? this.timerPayload(sm, s.fault) : { ...this.timerPayload(sm, false), is_endgame: false };
        }
      }
      this.send(ws, 'screen_state', out);
    }
    const m = s.running ? await this.getMatch(s.matchId) : null;
    if (!m) return;
    this.send(ws, 'match_started', { ...this.matchDict(m), status: 'in_progress' });
    this.send(ws, 'timer_update', this.timerPayload(m, s.fault));
  }

  // ---------- match clock (1s alarms) ----------

  async alarm() {
    const s = await this.state();
    if (s.clearEndgame) {
      s.clearEndgame = false;
      await this.setState(s);
      this.broadcast('clear_endgame', { match_id: s.matchId });
      return;
    }
    if (!s.running) return;
    const m = await this.getMatch(s.matchId);
    if (!m) return this.setState({ ...s, running: false });

    if (!s.fault) {
      m.match_time_remaining = Math.max(m.match_time_remaining - 1, 0);
      const payload = this.timerPayload(m, false);
      await this.run('UPDATE match SET match_time_remaining = ?, is_endgame = ? WHERE id = ?', m.match_time_remaining, +payload.is_endgame, m.id);
      this.broadcast('timer_update', payload);

      if (m.match_time_remaining === 0) {
        await this.run("UPDATE match SET status = 'completed', end_time = ? WHERE id = ?", now(), m.id);
        await this.calculateRps(m.id);
        this.broadcast('match_ended', { match_id: m.id });
        await this.setState({ ...s, running: false, clearEndgame: true });
        await this.ctx.storage.setAlarm(Date.now() + 1000); // clear the endgame banner 1s later
        return;
      }
    }
    // Schedule from the planned tick, not "now", so the clock doesn't drift.
    s.nextTick = Math.max(s.nextTick + 1000, Date.now() + 200);
    await this.setState(s);
    await this.ctx.storage.setAlarm(s.nextTick);
  }
}

// ---------- socket event handlers (all require the event PIN) ----------

const HANDLERS = {
  /** One scoring tap. Live FUEL is sent by HUB side and the server works out alliance + AUTO/TELEOP from the clock. */
  async score_event(data, ws) {
    const m = await this.getMatch(toInt(data.match_id));
    if (!m) return;
    let alliance = data.alliance;
    let type = data.event_type;
    let details = null;
    if (type === 'fuel') {
      if (data.hub_side !== 'red' && data.hub_side !== 'blue') return;
      [alliance, type] = game.creditFuel(data.hub_side, game.MATCH_LENGTH - m.match_time_remaining);
      details = `${data.hub_side} side of HUB`;
    }
    let points;
    if (type === 'connection') { alliance = null; points = 0; }
    else if (game.COUNT_TYPES.includes(type) && (alliance === 'red' || alliance === 'blue')) points = game.POINTS[type] ?? game.FOUL_POINTS[type];
    else return;

    const { meta } = await this.run('INSERT INTO match_event (match_id, event_type, alliance, points, details) VALUES (?, ?, ?, ?, ?)', m.id, type, alliance, points, details);
    await this.recount(m.id);
    this.send(ws, 'event_recorded', { event_id: meta.last_row_id }); // sender only, powers its Undo button
    this.broadcast('score_updated', this.matchDict(await this.getMatch(m.id)));
  },

  async delete_event(data) {
    const ev = await this.first('SELECT * FROM match_event WHERE id = ?', toInt(data.event_id));
    if (!ev || ev.match_id !== toInt(data.match_id)) return;
    await this.run('DELETE FROM match_event WHERE id = ?', ev.id);
    await this.recount(ev.match_id);
    const payload = this.matchDict(await this.getMatch(ev.match_id));
    this.broadcast('event_deleted', payload);
    this.broadcast('score_updated', payload);
  },

  async fta_save_match(data, ws) {
    const matchType = data.match_type || 'Qualification';
    const matchNumber = toInt(data.match_number) || (await this.nextMatchNumber());
    let id = toInt(data.match_id);
    if (!id || !(await this.getMatch(id))) {
      ({ meta: { last_row_id: id } } = await this.run('INSERT INTO match (match_number, match_type) VALUES (?, ?)', matchNumber, matchType));
    }
    const teams = [];
    for (const k of TEAM_KEYS) { const n = toInt(data[k]); teams.push(n ? await this.teamId(n) : null); }
    await this.run('UPDATE match SET match_number = ?, match_type = ?, red_team1_id = ?, red_team2_id = ?, blue_team1_id = ?, blue_team2_id = ? WHERE id = ?', matchNumber, matchType, ...teams, id);
    this.send(ws, 'match_data_saved', { match_id: id });
  },

  async fta_ready_display() {
    await this.setScreen({ name: 'ready', review: false });
    this.broadcast('show_ready_for_match');
  },

  async fta_update_display(data) {
    const prematch = {
      match_type: data.match_type || '?',
      match_number: data.match_number || '?',
      red_team1: data.red_team1 || '????',
      red_team2: data.red_team2 || '????',
      blue_team1: data.blue_team1 || '????',
      blue_team2: data.blue_team2 || '????',
    };
    await this.setScreen({ name: 'prematch', prematch, review: false });
    this.broadcast('show_prematch', prematch);
  },

  async fta_start_match(data) {
    const quickStart = Boolean(data.quick_start); // jump to the last 15s of TELEOP
    let id = toInt(data.match_id);
    const existing = id ? await this.getMatch(id) : null;
    if (!existing) {
      const number = toInt(data.match_number) || (await this.nextMatchNumber());
      ({ meta: { last_row_id: id } } = await this.run('INSERT INTO match (match_number) VALUES (?)', number));
    }
    for (const k of TEAM_KEYS) {
      const n = toInt(data[k]);
      if (n) await this.run(`UPDATE match SET ${k}_id = ? WHERE id = ?`, await this.teamId(n), id);
      else if (!existing) await this.run(`UPDATE match SET ${k}_id = NULL WHERE id = ?`, id);
    }
    const remaining = quickStart ? 15 : game.MATCH_LENGTH;
    const [period] = game.matchState(game.MATCH_LENGTH - remaining);
    // A (re)start is a fresh match: drop events left over from an accidental start
    await this.run('DELETE FROM match_event WHERE match_id = ?', id);
    await this.run("UPDATE match SET match_type = ?, status = 'in_progress', start_time = ?, end_time = NULL, match_time_remaining = ?, is_endgame = ? WHERE id = ?",
      data.match_type || 'Qualification', now(), remaining, +(period === 'END GAME'), id);
    await this.recount(id);

    const nextTick = Date.now() + 1000;
    await this.setState({ matchId: id, running: true, fault: false, nextTick, clearEndgame: false });
    await this.ctx.storage.setAlarm(nextTick);

    const m = await this.getMatch(id);
    const payload = this.matchDict(m);
    await this.setScreen({ name: 'live', matchId: id, review: false });
    this.broadcast('show_live', payload);
    this.broadcast('timer_update', this.timerPayload(m, false));
    this.broadcast('match_started', { ...payload, status: 'in_progress' });
  },

  async fta_field_fault() {
    const s = await this.state();
    await this.setState({ ...s, fault: true });
    this.broadcast('field_fault_started', { match_id: s.matchId });
    const m = await this.getMatch(s.matchId);
    if (m) this.broadcast('timer_update', this.timerPayload(m, true));
  },

  async fta_resume_match() {
    const s = await this.state();
    await this.setState({ ...s, fault: false });
    this.broadcast('field_fault_ended', { match_id: s.matchId });
    const m = await this.getMatch(s.matchId);
    if (m) this.broadcast('timer_update', this.timerPayload(m, false));
  },

  async fta_stop_match(data) {
    const s = await this.state();
    const id = toInt(data.match_id) || s.matchId;
    if (!id) return;
    await this.setState({ ...s, running: false, fault: false });
    await this.ctx.storage.deleteAlarm();
    await this.run("UPDATE match SET status = 'stopped', match_time_remaining = 0, is_endgame = 0 WHERE id = ?", id);
    await this.setScreen({ name: 'prematch', review: false }); // display falls back to its pre-match screen
    this.broadcast('match_stopped', { match_id: id });
  },

  /** Finalize match after review - recalculate RPs with reviewed scores. */
  async fta_finalize_match(data) {
    const id = toInt(data.match_id);
    if (!(await this.getMatch(id))) return;
    await this.run("UPDATE match SET status = 'finalized' WHERE id = ?", id);
    await this.calculateRps(id);
    const m = await this.getMatch(id);
    this.broadcast('match_finalized', { match_id: id, red_score: m.red_score, blue_score: m.blue_score });
  },

  async fta_show_review(data) {
    await this.setScreen({ review: true });
    this.broadcast('show_review', { match_id: data.match_id ?? null });
  },

  async fta_hide_review() {
    await this.setScreen({ review: false });
    this.broadcast('hide_review');
  },

  review_complete(data) {
    this.broadcast('review_complete', { match_id: data.match_id ?? null });
  },

  async fta_show_postmatch(data) {
    const id = toInt(data.match_id);
    const m = id ? await this.getMatch(id) : await this.first(`${MATCH_SELECT} ORDER BY m.id DESC LIMIT 1`);
    if (!m) return;
    if (m.status !== 'completed' && m.status !== 'finalized') {
      await this.run("UPDATE match SET status = 'completed', end_time = COALESCE(end_time, ?) WHERE id = ?", now(), m.id);
    }
    await this.calculateRps(m.id);
    await this.setScreen({ name: 'postmatch', matchId: m.id, review: false });
    this.broadcast('show_postmatch', this.matchDict(await this.getMatch(m.id)));
  },
};
