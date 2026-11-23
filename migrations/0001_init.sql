-- IRISH 2026 (BUILT) schema. Applied with `wrangler d1 migrations apply DB`.

CREATE TABLE team (
  id INTEGER PRIMARY KEY,
  number INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL
);

CREATE TABLE team_ranking (
  id INTEGER PRIMARY KEY,
  team_id INTEGER NOT NULL UNIQUE REFERENCES team(id),
  ranking_points INTEGER NOT NULL DEFAULT 0,
  previous_rank INTEGER,
  current_rank INTEGER
);

CREATE TABLE match (
  id INTEGER PRIMARY KEY,
  match_number INTEGER NOT NULL,
  match_type TEXT NOT NULL DEFAULT 'Qualification',
  red_team1_id INTEGER REFERENCES team(id),
  red_team2_id INTEGER REFERENCES team(id),
  blue_team1_id INTEGER REFERENCES team(id),
  blue_team2_id INTEGER REFERENCES team(id),
  red_score INTEGER NOT NULL DEFAULT 0,
  blue_score INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'scheduled',
  start_time TEXT,
  end_time TEXT,
  match_time_remaining INTEGER NOT NULL DEFAULT 163, -- ticks left of AUTO + delay + TELEOP
  is_endgame INTEGER NOT NULL DEFAULT 0,
  -- scoring counts (point values live in src/game.js); *_foul = committed BY that alliance
  red_auto_fuel INTEGER NOT NULL DEFAULT 0,
  red_teleop_fuel INTEGER NOT NULL DEFAULT 0,
  red_leave INTEGER NOT NULL DEFAULT 0,
  red_park INTEGER NOT NULL DEFAULT 0,
  red_minor_foul INTEGER NOT NULL DEFAULT 0,
  red_major_foul INTEGER NOT NULL DEFAULT 0,
  blue_auto_fuel INTEGER NOT NULL DEFAULT 0,
  blue_teleop_fuel INTEGER NOT NULL DEFAULT 0,
  blue_leave INTEGER NOT NULL DEFAULT 0,
  blue_park INTEGER NOT NULL DEFAULT 0,
  blue_minor_foul INTEGER NOT NULL DEFAULT 0,
  blue_major_foul INTEGER NOT NULL DEFAULT 0,
  connection INTEGER NOT NULL DEFAULT 0, -- all 4 robots; both alliances earn the RP
  red_auto_rp INTEGER NOT NULL DEFAULT 0,
  red_fuel_rp INTEGER NOT NULL DEFAULT 0,
  blue_auto_rp INTEGER NOT NULL DEFAULT 0,
  blue_fuel_rp INTEGER NOT NULL DEFAULT 0,
  red_rp INTEGER NOT NULL DEFAULT 0,  -- RP already added to team rankings
  blue_rp INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE match_event (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES match(id),
  event_type TEXT NOT NULL,
  alliance TEXT,
  team_id INTEGER REFERENCES team(id),
  points INTEGER NOT NULL DEFAULT 0,
  timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  details TEXT
);

CREATE INDEX match_event_match ON match_event(match_id);
