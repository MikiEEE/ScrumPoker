# Scrum Poker App

A websocket scrum poker web app built on top of the included `SmallOS` framework.

The app now runs two isolated scrum poker boards on one cooperative SmallOS runtime: the root board at `/` and a second board at `/legalease`. A shared host task owns the listener, while each mounted board keeps its own state, websocket clients, idle timer, and admin/session controls.

## Features

- Join a session with your name
- Promote yourself to admin with the `ADMIN_PASSPHRASE` environment variable
- Cast point votes in real time
- See when teammates have voted without revealing hidden values
- Show or hide all votes on the board
- Discard the current round's votes
- Open and close joining from the browser as an admin
- Kick users off the board as an admin
- Show admins with a badge on the board
- Control whether new users can join from the SmallOS shell
- Includes a top-right `Powered by SmallOS` link to the upstream project
- Serves HTML, CSS, and JavaScript from external static asset files
- Keeps a browser session token so refreshes preserve the same joined/admin status
- Runs separate root and `/legalease` boards with isolated state on one runtime
- Uses one namespaced terminal shell to control both mounted boards

## Requirements

- Python 3

No extra dependencies are required for the scrum poker app itself.

## Run The App

From the project root:

```bash
cp .env.example .env
# edit .env and set ADMIN_PASSPHRASE
# optionally change HOST / PORT
python3 scrum_poker_app.py
```

Then open:

```text
http://127.0.0.1:8082/
http://127.0.0.1:8082/legalease
```

If you leave `HOST=0.0.0.0`, the app binds on all local interfaces so other devices on your LAN can reach it using your machine's local IP address and the configured `PORT`.

## Admin Shell

When the app starts, it also opens a SmallOS shell in the terminal.

Useful commands:

- `poker apps`: list mounted scrum poker boards
- `poker root session open`: allow new users to join the root board
- `poker root session close`: prevent new users from joining the root board
- `poker legalease session open`: allow new users to join the `/legalease` board
- `poker legalease session close`: prevent new users from joining the `/legalease` board
- `poker <app_id> idle status`: inspect the idle timer for one board
- `poker <app_id> clear everyone`: kick everyone off one board and close its session
- `ps`: list running SmallOS tasks
- `stat <pid>`: inspect a task
- `toggle`: switch between shell output and application output
- `help`: show available commands

## Browser Behavior

Users can:

- Enter a display name and join the session
- Click `Become Admin`, enter the configured passphrase, and receive success or failure feedback
- Vote using the on-screen cards
- See who has already voted
- Reveal or hide the board
- Clear the current round

Admins can:

- Open the session for new users
- Close the session for new users
- Kick users off the board
- Appear with an `Admin` badge beside their name

Refreshing the page in the same tab keeps the same browser session token, so the user stays associated with the same name, vote, and admin status during reconnects. Separate tabs use separate tab identities so they do not fight over the same websocket session.

The root board and the `/legalease` board also use different browser storage keys, so you can keep both open in one browser without their names or reconnect tokens colliding.

While votes are hidden:

- each user still sees their own vote
- other users only appear as "voted" or "waiting"

## Routes

- `/`: root board UI
- `/ws`: root board WebSocket endpoint
- `/api/state`: root board JSON snapshot
- `/healthz`: root board health check
- `/static/app.css` and `/static/app.js`: root board assets
- `/legalease`: second board UI
- `/legalease/ws`: second board WebSocket endpoint
- `/legalease/api/state`: second board JSON snapshot
- `/legalease/healthz`: second board health check
- `/legalease/static/app.css` and `/legalease/static/app.js`: second board assets

## Tests

Focused scrum poker tests:

```bash
python3 -m unittest tests.test_scrum_poker_app tests.test_smallos_websocket_server -v
```

Full SmallOS test suite:

```bash
cd SmallOS
python3 -m unittest discover -s tests -v
```

## Project Structure

- [`scrum_poker_app.py`](./scrum_poker_app.py): app entrypoint and compatibility import surface
- [`scrum_poker_core.py`](./scrum_poker_core.py): shared poker state helpers, HTTP helpers, dotenv loading, and runtime wiring utilities
- [`scrum_poker_board.py`](./scrum_poker_board.py): mounted scrum poker board class and idle watchdog
- [`scrum_poker_host.py`](./scrum_poker_host.py): shared host/router class for mounted boards
- [`scrum_poker_shell.py`](./scrum_poker_shell.py): multi-board SmallOS shell commands
- [`smallos_websocket_server.py`](./smallos_websocket_server.py): local SmallOS-friendly websocket server helper used by the app
- [`.env.example`](./.env.example): sample environment file for `ADMIN_PASSPHRASE` and optional origin restrictions
- [`static/index.html`](./static/index.html): main browser markup
- [`static/app.css`](./static/app.css): UI styling, including instance badge styling for mounted boards
- [`static/app.js`](./static/app.js): client-side websocket and UI logic with per-instance bootstrap config
- [`SmallOS/`](./SmallOS): bundled SmallOS framework used by the app
- [`tests/test_scrum_poker_app.py`](./tests/test_scrum_poker_app.py): tests for poker-specific state and shell controls
- [`tests/test_smallos_websocket_server.py`](./tests/test_smallos_websocket_server.py): tests for the local websocket server helper
