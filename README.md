# Scrum Poker App

A websocket scrum poker web app built on top of the included `SmallOS` framework.

The app now runs one premium permanent room plus a pool of ephemeral GUID rooms on one cooperative SmallOS runtime. A shared host task owns the listener, public landing/setup flow, room registry, and expiry cleanup.

## Features

- Join a session with your name
- Create ephemeral rooms from `/setupRoom`
- Auto-claim admin in the room you just created
- Promote yourself to admin with the room password or `SUPER_USER_PASSPHRASE`
- Keep `/legalease` locked behind its premium admin password
- Cast point votes in real time
- See when teammates have voted without revealing hidden values
- Show or hide all votes on the board
- Discard the current round's votes
- Open and close joining from the browser as an admin
- Kick users off the board as an admin
- Show admins with a badge on the board
- Control room sessions from the SmallOS shell
- Includes a top-right `Powered by SmallOS` link to the upstream project
- Serves HTML, CSS, and JavaScript from external static asset files
- Keeps a browser session token so refreshes preserve the same joined/admin status
- Destroys expired ephemeral rooms fully so room state does not accumulate in memory
- Uses one namespaced terminal shell to control the premium room and active GUID rooms

## Requirements

- Python 3

No extra dependencies are required for the scrum poker app itself.

## Run The App

From the project root:

```bash
cp .env.example .env
# edit .env and set LEGALEASE_ADMIN_PASSPHRASE / SUPER_USER_PASSPHRASE
# optionally change HOST / PORT
python3 app.py
```

Then open:

```text
http://127.0.0.1:8082/
http://127.0.0.1:8082/setupRoom
http://127.0.0.1:8082/legalease
```

If you leave `HOST=0.0.0.0`, the app binds on all local interfaces so other devices on your LAN can reach it using your machine's local IP address and the configured `PORT`.

## Admin Shell

When the app starts, it also opens a SmallOS shell in the terminal.

Useful commands:

- `poker rooms`: list the premium room and every active ephemeral GUID room
- `poker apps`: alias for `poker rooms`
- `poker legalease session open`: allow new users to join the `/legalease` board
- `poker legalease session close`: prevent new users from joining the `/legalease` board
- `poker <guid> session open|close|status|toggle`: manage an active ephemeral room
- `poker <room_id> idle status`: inspect the idle timer for one room
- `poker <room_id> clear everyone`: kick everyone off one room and close its session
- `ps`: list running SmallOS tasks
- `stat <pid>`: inspect a task
- `toggle`: switch between shell output and application output
- `help`: show available commands

## Browser Behavior

Users can:

- Visit `/setupRoom`, choose a room password, and get redirected into a private GUID room
- Enter a display name and join the session
- Click `Become Admin`, enter the room password, and receive success or failure feedback
- Vote using the on-screen cards
- See who has already voted
- Reveal or hide the board
- Clear the current round

Admins can:

- Open the session for new users
- Close the session for new users
- Kick users off the board
- Appear with an `Admin` badge beside their name

The creator of a new ephemeral room is auto-promoted to admin in that browser session. Premium `/legalease` admin access uses `LEGALEASE_ADMIN_PASSPHRASE` with `ADMIN_PASSPHRASE` as a fallback, and `SUPER_USER_PASSPHRASE` works everywhere.

Refreshing the page in the same tab keeps the same browser session token, so the user stays associated with the same name, vote, and admin status during reconnects. Separate tabs use separate tab identities so they do not fight over the same websocket session.

Each GUID room and `/legalease` use different browser storage keys, so you can keep multiple rooms open in one browser without their names or reconnect tokens colliding.

While votes are hidden:

- each user still sees their own vote
- other users only appear as "voted" or "waiting"

Ephemeral room limits:

- max `19` active public rooms at once
- max `8` named participants per ephemeral room
- hard expiry after `2` hours from creation
- idle destruction after `1` hour with no activity

Premium room limits:

- `/legalease` keeps its fixed URL
- max `20` named participants

## Routes

- `/`: landing page
- `/setupRoom`: public room setup page
- `/api/rooms`: create a new ephemeral room
- `/healthz`: host health check
- `/legalease`: premium room UI
- `/legalease/ws`: premium room WebSocket endpoint
- `/legalease/api/state`: premium room JSON snapshot
- `/legalease/healthz`: premium room health check
- `/<guid>`: ephemeral room UI
- `/<guid>/ws`: ephemeral room WebSocket endpoint
- `/<guid>/api/state`: ephemeral room JSON snapshot
- `/<guid>/healthz`: ephemeral room health check
- `/static/app.css` and `/static/setup_room.js`: host-level setup/landing assets
- `/<room>/static/app.css` and `/<room>/static/app.js`: room assets

## Tests

Focused scrum poker tests:

```bash
python3 -m unittest tests.test_scrum_poker_app tests.test_smallos_websocket_server -v
```

Local benchmark helper:

```bash
python3 benchmark_scrum_poker.py
```

Full SmallOS test suite:

```bash
cd SmallOS
python3 -m unittest discover -s tests -v
```

## Project Structure

- [`app.py`](./app.py): executable SmallOS entrypoint and premium-room composition
- [`scrum_poker_app.py`](./scrum_poker_app.py): mounted scrum poker room class and idle watchdog
- [`scrum_poker_core.py`](./scrum_poker_core.py): shared poker state helpers, HTTP helpers, dotenv loading, and runtime wiring utilities
- [`scrum_poker_host.py`](./scrum_poker_host.py): shared host/router, public setup flow, and ephemeral room registry
- [`scrum_poker_shell.py`](./scrum_poker_shell.py): SmallOS shell commands for premium and GUID rooms
- [`benchmark_scrum_poker.py`](./benchmark_scrum_poker.py): lightweight benchmark for room fanout behavior
- [`smallos_websocket_server.py`](./smallos_websocket_server.py): local SmallOS-friendly websocket server helper used by the app
- [`.env.example`](./.env.example): sample environment file for premium admin, super-user access, and global limits
- [`static/index.html`](./static/index.html): room page markup
- [`static/landing.html`](./static/landing.html): public landing page
- [`static/setup_room.html`](./static/setup_room.html): room setup page
- [`static/app.css`](./static/app.css): shared UI styling
- [`static/app.js`](./static/app.js): client-side websocket and room UI logic
- [`static/setup_room.js`](./static/setup_room.js): room creation flow
- [`SmallOS/`](./SmallOS): bundled SmallOS framework used by the app
- [`tests/test_scrum_poker_app.py`](./tests/test_scrum_poker_app.py): tests for room creation, auth, cleanup, and shell controls
- [`tests/test_smallos_websocket_server.py`](./tests/test_smallos_websocket_server.py): tests for the local websocket server helper
