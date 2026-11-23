# IRISH Control Panel

Field management system for IRISH 2026 (**BUILT**), Team 8866's off-season event. A web-based control system for FIRST Robotics Competition (FRC) events, providing real-time match management, scoring, and display capabilities.

## Capabilities

### Match Management
- Complete match setup with team assignments
- One continuous match timer: AUTO 0:20, 3s scoring delay, TELEOP 2:20 (TRANSITION, SHIFT 1-4, END GAME)
- Field fault handling and timer pause/resume
- Ability to stop match and reset incase of accidental start (restart clears that match's events)
- Match review and post-match analysis

### Scoring System
- FUEL scored by HUB side: the FMS credits the right alliance from the shift table, with the 3s grace after each swap
- AUTO FUEL 4 pts, TELEOP FUEL 2 pts, LEAVE 3 pts, PARK 2 pts
- MINOR FOUL +2 / MAJOR FOUL +6 to the opponent
- Point values and the FUEL BONUS threshold live in `src/game.js`

### Ranking Points
- Win 3 / Tie 1, AUTO BONUS (both robots LEAVE + 5 AUTO FUEL), FUEL BONUS (80 FUEL, edit `FUEL_BONUS_THRESHOLD` in `src/game.js`), CONNECTION (both alliances)
- Only qualification matches add RP to rankings
- Run `npm test` to check the rules

### Interfaces
- **FTA Panel**: Match setup, timer control, field fault management
- **Referee Panel** (phone-first): pick a role (Red HUB / Blue HUB / Referee / All), big buttons, Undo for your own taps
- **Display Panel**: Real-time scoreboard with pre-match, live, and post-match views

### Data & Communication
- Cloudflare D1 database for teams, matches, events and rankings
- WebSocket-based real-time updates across all interfaces
- Event logging and match history
- Audio notifications for match events

## Live site

- Audience display: https://irish.frc8866.org/
- FTA: https://irish.frc8866.org/fta · Referees/scorers: https://irish.frc8866.org/referee · Admin: https://irish.frc8866.org/admin · Rankings: https://irish.frc8866.org/rankings
- `irishcontrol.pages.dev` only redirects to irish.frc8866.org.

FTA, referee and admin pages ask for the **event PIN** once per device (set it with `npx wrangler secret put EVENT_PIN`; use a long one). Display and rankings are public.

## How it runs (Cloudflare)

- **Worker** `irishcontrol` (`src/index.js`) serves the pages in `public/` and forwards `/api/*` and `/ws` to...
- **Durable Object** `Field`: holds every WebSocket, runs the match clock (1s alarms), handles scoring and the API, so everything happens in order.
- **D1** `irishcontrol`: schema in `migrations/`.
- Pages talk to it through `public/static/js/socket-shim.js` (a tiny `io()` stand-in over plain WebSocket that reconnects on its own).

## Commands (Windows PowerShell)

Install once: `npm install`

| Task | Command |
| --- | --- |
| Run everything locally (offline backup at the venue) | `npm run dev` then open http://localhost:8787 (PIN comes from `.dev.vars`) |
| Check the game rules | `npm test` |
| Deploy to Cloudflare | `npm run deploy` |
| Deploy the pages.dev redirect | `npm run deploy:pages` |
| Set / change the event PIN | `npx wrangler secret put EVENT_PIN` |

For local dev create `.dev.vars` containing `EVENT_PIN=something` (it's git-ignored).

## Technology Stack

- **Backend**: Cloudflare Workers + Durable Objects
- **Database**: Cloudflare D1 (SQLite)
- **Frontend**: HTML/CSS/JavaScript
- **Real-time**: WebSockets (Durable Object hibernation API)
