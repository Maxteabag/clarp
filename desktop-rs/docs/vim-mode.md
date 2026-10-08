# Vim mode (desktop)

Vim mode lets you use the whole window without Ctrl. It is on by default.
Turn it off with Settings → Behaviour → Vim mode, `:set novim`, or "Vim
mode" in the command palette. Every Ctrl binding still works, and keys you
bind yourself in the key bindings editor take priority over vim keys.

While you type (the composer, the explorer's search) you are in Insert mode
and every letter types. Everywhere else, the chat and the explorer are in
Normal mode. The bottom bar and the pane's frame title show the mode:
NORMAL, INSERT, COMMAND or SEARCH.

Key names follow Vim: `j` is the key on its own, `J` is Shift+j, and `Space`
is the space bar. A number typed first repeats the key or picks an item:
`3j`, `2gt`.

## Modes

| Keys | What it does |
|---|---|
| `i` `a` `o` | Insert: the composer |
| `Esc` | Back to Normal (in Normal it never stops an agent; use `Space x`) |
| `:` | Command line (Tab completes, Esc cancels) |
| `/` | Search the chat |
| `?` | The keys that matter here (chat, card, explorer, a surface), including your own bindings; `?` or `Esc` closes it. Works with vim mode off too; in a text field it types `?` |

## Chat

| Keys | What it does |
|---|---|
| `j` / `k` | Scroll a line (`5j`) |
| `d` / `u` | Half a page down or up |
| `gg` / `G` | Top or latest |
| `[` / `]` | Previous or next turn (your messages) |
| `/text` Enter, `n` / `N` | Search; next or previous match (a capital letter makes the search case-sensitive) |
| `y` | Copy the message under the cursor (the latest one at the end) |
| `zo` `zc` `za` | Open, close or toggle the tool activity under the cursor |
| `J` / `K` | Onto the artifact cards; on a card `j`/`k` walk, `o` opens, digits choose, `Esc` leaves |
| `f` | Link hints |
| `h` | The explorer |
| `gt` / `gT`, `2gt` | Next, previous or second tab |

## Explorer

| Keys | What it does |
|---|---|
| `j` / `k` | Next or previous chat |
| `l` | Unfold sub-agents, or open the chat (keyboard on the chat) |
| `Enter` | Open the chat in Insert |
| `/` | Filter |
| `p` | Live preview |
| `zo` / `zc` | Unfold or fold sub-agents |
| `gg` / `G` | First or last chat |
| `h` | The chat |

## Leader: `Space`

After `Space` a panel lists the next keys.

| Keys | What it does |
|---|---|
| `Space Space` | Command palette |
| `Space e` / `Space b` | Explorer / show or hide it |
| `Space n` / `Space N` | New agent / start a contact |
| `Space s` / `Space k` | Settings / key bindings |
| `Space t` | New tab |
| `Space r` / `Space a` | Recent agents / next attention |
| `Space /` | Search the messages of every chat (Ctrl+F) |
| `Space u` / `Space T` / `Space o` | Updates / Teams / Overview |
| `Space p` / `Space R` / `Space d` | Agent profile / rename / change directory |
| `Space m` / `Space x` / `Space l` | Mute / stop the agent / open a link |
| `Space ?` | Show or hide the key bar |
| `Space w v` / `Space w s` | Split right / down |
| `Space w h/j/k/l` | Move between panes |
| `Space w q` / `z` / `=` | Close / zoom / balance panes |
| `Space w n` / `]` / `[` / `c` | New / next / previous / close tab |

## Commands

| Command | What it does |
|---|---|
| `:q`, `:close` | Close the pane |
| `:vs`, `:sp` | Split right, split down |
| `:only` | Zoom the pane |
| `:tabnew [name]`, `:tabn`, `:tabp`, `:tabc` | Tabs |
| `:e name`, `:b name` | Open an agent's chat (Tab completes names) |
| `:theme hacker`, `:colo paper` | Reading theme (Tab lists them) |
| `:font` / `:font reset` | Pick the reading theme's font / back to the theme's own |
| `:font +`, `:font -`, `:font =`, `:font 120` | Chat zoom (Ctrl+= / Ctrl+- / Ctrl+0) |
| `:set novim` / `vim` / `vim!` | Vim mode off, on, toggle |
| `:set name=value`, `name+=2`, `noname`, `name!` | Any preference by name (`fontsize=16`, `notimestamps`, `timeformat=24h`); Tab completes the names |
| `:noh` | Forget the search |
| `:settings`, `:keymap` (`:help`), `:new`, `:recent`, `:updates`, `:teams`, `:overview` | Open them |

## Tabs and the explorer with Ctrl

`Ctrl+T` opens a new tab (in the New Session window it is still "Show all"),
`Ctrl+Tab` / `Ctrl+Shift+Tab` go to the next or previous tab, and `Ctrl+W`
closes the tab from the chat or the explorer (not while you type).
`Ctrl+Shift+W` still closes a pane. `Ctrl+B` shows the explorer with the
keyboard on the open chat. Pressing it again hides the explorer and moves
the keyboard back to the chat.
