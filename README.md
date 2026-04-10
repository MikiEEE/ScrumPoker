# Scrum Poker App

A websocket scrum poker web app built on top of the included `SmallOS` framework.

The app runs a cooperative HTTP + WebSocket server from [`scrum_poker_app.py`](./scrum_poker_app.py) and keeps the browser UI plus the terminal admin shell in sync through shared in-memory state.

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

## Requirements

- Python 3

No extra dependencies are required for the scrum poker app itself.

## Run The App

From the project root:

```bash
cp .env.example .env
# edit .env and set ADMIN_PASSPHRASE
python3 scrum_poker_app.py
```

Then open:

```text
http://127.0.0.1:8082/
```

## Admin Shell

When the app starts, it also opens a SmallOS shell in the terminal.

Useful commands:

- `poker`: list ScrumPokerShell-specific commands
- `session open`: allow new users to join
- `session close`: prevent new users from joining
- `session status`: show whether joining is open or closed
- `session toggle`: flip the join state
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

While votes are hidden:

- each user still sees their own vote
- other users only appear as "voted" or "waiting"

## Routes

- `/`: main web UI
- `/ws`: WebSocket endpoint for live session updates
- `/api/state`: JSON snapshot of the current session
- `/healthz`: simple health check

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

- [`scrum_poker_app.py`](./scrum_poker_app.py): app entrypoint, HTTP server, scrum poker state, admin auth, and custom shell commands
- [`smallos_websocket_server.py`](./smallos_websocket_server.py): local SmallOS-friendly websocket server helper used by the app
- [`.env.example`](./.env.example): sample environment file for `ADMIN_PASSPHRASE`
- [`static/index.html`](./static/index.html): main browser markup
- [`static/app.css`](./static/app.css): UI styling
- [`static/app.js`](./static/app.js): client-side websocket and UI logic
- [`SmallOS/`](./SmallOS): bundled SmallOS framework used by the app
- [`tests/test_scrum_poker_app.py`](./tests/test_scrum_poker_app.py): tests for poker-specific state and shell controls
- [`tests/test_smallos_websocket_server.py`](./tests/test_smallos_websocket_server.py): tests for the local websocket server helper
