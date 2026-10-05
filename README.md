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
`save` also looks up the account email. If that lookup fails, it still saves the
credentials and reports the lookup failure.

## Email labels and usage

```sh
kiro-accounts list
kiro-accounts usage
```

`list` shows the email for each saved alias. Existing snapshots without an email
are looked up automatically on their first listing; subsequent listings use the
cached email. Refresh all labels explicitly with `kiro-accounts list --refresh`.

`usage` queries every **saved** account and shows its email, plan, quota,
used credits, credit limit, remaining credits, billing reset date, and status.
It queries Kiro's live usage service through ACP, without sending model prompts.
Each account uses its own temporary Kiro data/config directory; the live login,
account selection, and T3 sessions remain unchanged. Kiro processes used for
the query are cleaned up afterward. Email labels and any refreshed credentials
are saved back to that account's snapshot.

Example output (illustrative):

```text
Account   Email             Plan       Quota    Used    Limit  Remaining  Reset       Status
personal  you@example.com   KIRO PRO+  Credits  222.74  2,000  1,777.26   2026-11-01  ok
```

Kiro determines which quotas and reset dates are available. Missing values are
shown as `—`, rather than guessed. Separate trial/bonus quotas are shown when
returned with numeric limits. Accounts with expired logins show `login required`;
other accounts are still checked. Errors and unavailable usage make the command
exit with status 1, while retaining successful rows.

For scripting and slower connections:

```sh
kiro-accounts usage --json
kiro-accounts list --json
kiro-accounts --timeout 60 usage
kiro-accounts --kiro-binary /path/to/kiro-cli usage
```

Queries run sequentially with a default timeout of 45 seconds per account.
No extra packages or T3 Code modifications are required.

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

The helper reports credit balances but does not automatically switch accounts
when credits run out or retry prompts.
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
Do not share or commit that directory. Identity and usage lookups authenticate
with Kiro's services through the installed CLI; snapshots are not uploaded to
any other service. Temporary credential copies are stored beneath a private
0700 directory and deleted after each query.
The recovery file holds only the credentials from before the most recent switch.

For development, `--db PATH` and `--store PATH` select alternate paths.

```sh
python3 -m unittest discover -s tests -v
```
