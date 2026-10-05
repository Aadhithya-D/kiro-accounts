# Kiro account switcher

A small, dependency-free Linux helper for the Kiro CLI 2.7.0 database layout.
Requires Python 3 and an existing Kiro CLI installation.

## Install

```sh
git clone https://github.com/Aadhithya-D/kiro-accounts.git
cd kiro-accounts
chmod +x kiro_accounts.py
mkdir -p ~/.local/bin
ln -s "$PWD/kiro_accounts.py" ~/.local/bin/kiro-accounts
```

Ensure `~/.local/bin` is on your `PATH`. Keep the checkout in place because
the installed command links to it. If the command already exists, inspect it
before replacing it.

## Register accounts once

Save the account currently logged in:

```sh
kiro-accounts save personal
```

Close Kiro chat sessions, log into the second account using Kiro's normal login,
then save that login:

```sh
kiro-cli logout
kiro-cli login --use-device-flow
kiro-accounts save second
```

Repeat for additional accounts. Account names are local aliases; no passwords
are requested by this helper. Saving an existing alias replaces its saved login.

## Switch accounts

Close running Kiro CLI / T3 Kiro sessions first, then:

```sh
kiro-accounts list
kiro-accounts use personal
# Or cycle through saved accounts in alphabetical order:
kiro-accounts next
kiro-cli whoami
```

Reopen Kiro chat or the T3 Kiro provider session afterward. The switch applies
globally to this Linux user's Kiro processes. The helper refuses a switch while
it detects a running Kiro process; it does not terminate processes for you.
`list` shows the last selected alias, which may be stale after a manual login.

Kiro can refresh login tokens while running. **Save the current account again
under its own alias before switching away** to retain those refreshed tokens.
If a saved session expires or is revoked, log into that account again and save
it. This uses Kiro's internal storage format, not an official account profile API;
future releases may need compatibility changes.

## T3 Code

This machine's installed `@adithyasak/t3` 0.0.45-adi.1 build already includes a
Kiro driver that launches `kiro-cli acp`. Leave its Kiro binary path unchanged.
Select an account with this helper, then start a Kiro session in T3 Code.
No source changes or protocol shim are required for this global switching flow.
An existing ACP process must be closed and restarted to pick up the new login.
Concurrent sessions with separate accounts and switching within an active
conversation are outside this initial helper's scope.

The helper does not detect exhausted credits or retry prompts automatically.
Use `next` when you need another account. Automated retry would need explicit
quota detection and session recovery so partially executed tool calls aren't
repeated.

## Storage and recovery

Only the `auth_kv` rows and the three account state keys are copied:
`api.codewhisperer.profile`, `auth.idc.region`, and `auth.idc.start-url`.
Chat history, agents, MCP configuration, and unrelated settings are preserved.
Live database updates use a SQLite transaction. Each switch stores the previous
credentials in a recovery file:

```sh
kiro-accounts restore
```

Snapshots contain sensitive login tokens, stored locally without encryption in
`~/.local/share/kiro-accounts/`, with directory permissions 0700 and files 0600.
Do not share or commit that directory. Nothing is sent to a server by the helper.
The recovery file holds only the credentials from before the most recent switch.

For development, `--db PATH` and `--store PATH` select alternate paths.

```sh
python3 -m unittest discover -s tests -v
```
