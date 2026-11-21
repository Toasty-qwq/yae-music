# Yae Music

A Discord music bot that plays **YouTube** and **Spotify** songs, albums and playlists in voice
channels, with a **web control panel** for logs, start/stop, settings and live "now playing" controls.

Runs on your Windows PC (`start.bat`) or as a Docker container (CasaOS or any Docker host).

## 1. Create the Discord bot (once)

1. Go to <https://discord.com/developers/applications> → **New Application** and give it a name.
2. Open **Bot** → **Reset Token** → copy the token. Keep it secret: anyone with it controls the bot.
   No "Privileged Gateway Intents" are needed.
3. Start the panel (step 2 or 3 below), open **Settings**, paste the token, then **Save** and **Start**.
4. On the panel's **Overview**, press **Open the invite** and pick your server.

## 2. Run it on your PC (Windows)

Needs **Python 3.11+** (from python.org, tick "Add to PATH") and **FFmpeg** (`winget install Gyan.FFmpeg`).

Double-click **`start.bat`**. The first run installs everything into `.venv` and then opens the
panel at <http://localhost:8484>. On first visit you choose a panel password. The bot runs for as
long as that window stays open.

## 3. Run it on CasaOS (Docker)

CasaOS installs apps from ready-made images. GitHub builds this one for you on every push, for
both normal PCs (amd64) and ARM boards like a Raspberry Pi 4/5 (arm64).

**Once: put the code on GitHub**

1. Make a free account at <https://github.com> and create a new repository named `yae-music`
   (public is simplest; see the note below for private). Don't add a README or licence.
2. In this folder, push the code (GitHub shows the same commands on the empty repository's page):

   ```bash
   git remote add origin https://github.com/YOUR-NAME/yae-music.git
   git push -u origin main
   ```

3. Open the repository's **Actions** tab. The **Build the CasaOS image** run takes about 5 minutes
   the first time. When it's green, open the run: its summary page shows the CasaOS compose file,
   already filled in with your image's name.
4. Make the image public, or CasaOS can't download it: your GitHub profile → **Packages** →
   `yae-music` → **Package settings** → **Change visibility** → **Public**.

**Install it in CasaOS**

5. CasaOS → **App Store** → **Custom Install** → **Import** (top right). Paste the compose
   file from step 3, then **Submit** and **Install**.
6. Click the **Yae Music** tile (or open `http://<server-ip>:8484`), choose a panel password,
   paste the token in **Settings**, **Save**, and press **Start**.

It starts by itself after a reboot. The token, settings, logs and cookies live in
`/DATA/AppData/yae-music` on the server, so they survive updates and reinstalls.

**To update:** push your changes (`git push`), wait for the Actions run to go green, then in CasaOS
use the update option in the app tile's menu, or uninstall and install it again. Your data
folder is kept either way.

**Private repository?** That works too. The code stays private, and only the image (step 4) is public.
The app icon then won't load in CasaOS, because it's read from the repository.

**Without GitHub:** copy this folder to the server and run `sudo docker compose up -d --build`
in it over SSH. That uses the root `docker-compose.yml`, which builds the image on the server itself.

## Discord commands

Join a voice channel first. The bot joins the channel of whoever uses `/play`.

| Command | What it does |
|---|---|
| `/play <link or words>` | YouTube video/playlist, Spotify track/album/playlist/artist, other yt-dlp sites, or a search |
| `/playnext <link or words>` | Same, but puts it at the front of the queue |
| `/skip` · `/skipto <n>` | Skip the song / jump to queue position *n* |
| `/stop` | Stop, clear the queue and leave |
| `/pause` · `/resume` | Pause / resume |
| `/nowplaying` | Current song with a progress bar |
| `/queue [page]` | Show the queue |
| `/volume [0-200]` | Show or set the volume |
| `/loop off\|song\|queue` | Looping |
| `/shuffle` · `/clear` | Shuffle / empty the queue |
| `/remove <n>` · `/move <from> <to>` | Edit the queue |
| `/seek <1:30>` | Jump inside the song |
| `/help` | List the commands |

The now-playing message has buttons: pause/resume, skip, stop, loop, shuffle. Only people in the
bot's voice channel can control it. The bot leaves after 5 idle minutes, or 5 minutes alone in the
channel (change this in Settings).

## The control panel

- **Servers**: each server's welcome, role, moderation and logging settings (see the next section).
- **Bot profile**: the bot's picture, banner, username, About me, online status and activity
  ("Listening to /play"), with a preview. Discord allows two username changes an hour.
- **Overview**: bot status, ping, server count, invite link, and every server that's playing,
  with pause/skip/stop/shuffle/loop/volume and queue removal.
- **Logs**: a live log with filtering and download. `docker logs musicbot` and CasaOS's own log
  view show the same lines.
- **Settings**: token, optional Spotify keys, default volume, idle timeout, playlist and queue
  limits, auto-start, auto-restart after crashes, and updating yt-dlp on start.
- **Upkeep**:
  - **Check-up** ("Find and fix problems"): checks the token and internet, intents, that the bot
    is online, ffmpeg, yt-dlp (and updates it), that a YouTube video plays, the Spotify keys, the
    database, disk space, permissions and roles in every server, stuck voice connections, and that
    the public address answers. It fixes what it can by itself (starts or restarts the bot, updates
    yt-dlp, clears stuck voice connections, deletes old logs when the disk is full) and says what to
    do about the rest.
  - **Scheduled restart**: every day, on chosen weekdays, once a month, or once on a date, at a
    time in your own time zone. It can update yt-dlp first and wait while someone is listening (2
    hours at most). Restarting doesn't change the ping, which depends on the internet connection.
  - Versions, **Update yt-dlp now**, YouTube cookies upload, panel password.

Forgot the panel password? Set `PANEL_PASSWORD` in `docker-compose.yml` (or as an environment
variable before `start.bat`). While it is set, it replaces the saved password.

## Server features (like Carl-bot)

Besides music, the bot does what Carl-bot does for a server. Every setting is per server, on the
panel's **Servers** page:

| Tab | What it does |
|---|---|
| Welcome | Welcome and farewell messages and a welcome DM, built with the **embed builder**. **Autorole** gives new members and bots roles, and can give people their roles back when they rejoin. Test with `/greetings testgreet` |
| Roles | **Reaction-role menus**: a message with buttons, a dropdown, or emoji reactions that members click to give themselves roles. Modes: toggle, one-at-a-time, or get-only |
| Forum roles | When a forum post gets a tag you pick (say "Accepted"), the person who made the post gets a role. Optionally the role goes again when the tag is removed, and the bot posts a message in the post or DMs the author. Make the tag moderator-only in the forum's settings so authors can't tag their own posts |
| Messages | Post your own messages and embeds (rules, announcements, info panels) and edit them later |
| Tags | Saved messages anyone posts with `/tag show <name>` |
| Auto-replies | When a message contains a trigger word, the bot answers with a message you build |
| Starboard | Messages with enough star reactions get reposted in a hall-of-fame channel |
| Suggestions | `/suggest` posts numbered ideas with vote reactions; `/suggestion approve/deny/consider/implemented` decides them |
| Moderation | `/ban` (also temporary: `1d`, `2w`), `/unban`, `/kick`, `/timeout`, `/untimeout`, `/warn`, `/warnings`, `/case`, `/reason`, `/purge`. Every action is a numbered case, listed on the dashboard and posted to a mod-log channel |
| Logging | Deleted and edited messages, joins and leaves, nickname and role changes, bans, kicks and timeouts (also ones done by hand), voice joins and moves, channel and role changes, each to its own channel |
| VC maker → Generators | "Join to create" voice channels (like VoiceMaster). Each generator has its own category, name pattern (`{username}'s room`, numbered, random animal/colour/tree), user limit and bitrate. Modes: a room per person, numbered rooms, a fixed list of names, a copy of the generator, or **dynamic** (always one empty room waiting). Rooms delete themselves when empty |
| VC maker → Controls | Which room controls owners get: rename, limit, lock, hide (ghost), permit/reject people, claim an abandoned room, status, "looking for members" posts, text chat, bitrate, invite, transfer, age-restrict, region. Also the control-panel message posted in each room, and whether owners get Manage Channel / Move Members |
| VC maker → Extras | A temporary role for people in rooms, a text channel per room (with a message you build), a log channel, and the "looking for members" channel. Owners can `/voice save` their room settings and `/voice load` them next time |
| Tickets → Panels | Ticket panels: one message with buttons or a dropdown, each a **ticket type** ("Support", "Apply to be mod", ...) with its own category or private thread, support roles, name pattern, opening message, up to 5 form questions, and a per-person limit. Edit a panel after posting and the message updates in place |
| Tickets → Tickets | Every open and closed ticket: who opened it, type, who claimed it, answers |
| Tickets → Settings | Log and transcript channels, ask-before-closing, keep closed tickets (reopen later) or delete them, HTML transcripts (also by DM), claiming, auto-close after inactivity, close when the owner leaves |

Room owners use `/voice` (`name`, `limit`, `lock`, `unlock`, `ghost`, `unghost`, `permit`,
`reject`, `claim`, `transfer`, `status`, `lfm`, `text`, `bitrate`, `invite`, `nsfw`, `region`,
`save`, `load`) or the buttons in their room. Tickets have `/ticket` (`close`, `closerequest`,
`reopen`, `delete`, `claim`, `add`, `remove`, `rename`, `transcript`).

Also for everyone: `/remind`, `/reminders`, `/userinfo`, `/serverinfo`, `/avatar`, and `/say` for
moderators. The panel's **Commands** page lists every command with what it does and who can use it.

**The embed builder** has every part of a Discord embed: text above it, author (icon, name, link),
title and title link, description, up to 25 fields (side by side or not), a big image, a
thumbnail, footer and footer icon, the time, and the colour, with a live preview. Image fields take
a link (GIFs from a direct `.gif` link work), a placeholder like `{user.avatar}` or `{server.icon}`, or
a picture or GIF up to 8 MB uploaded with the button next to the field. Placeholders work in Carl-bot's
style too: `{user}`, `{mention}`, `{user.name}` or `{user(name)}`, `{user.avatar}` or `{user(icon)}`,
`{server}`, `{server.icon}`, `{membercount}`, `{channel}`.

**One-time setup in the Discord Developer Portal** (<https://discord.com/developers/applications> → your bot):

1. **Bot** → **Privileged Gateway Intents** → turn on **Server Members Intent** and
   **Message Content Intent**, then **Save**. Without them the bot still plays music, but welcome
   messages, autorole and the logs stay off (the panel says so).
2. **Re-invite the bot** with the invite button on the panel's Overview. The new link asks for the
   extra permissions (Manage Roles, Kick, Ban, Timeout, Manage Messages, View Audit Log, Manage
   Channels, Move Members, Set Voice Channel Status, Manage Threads, Create Private Threads, Send
   Messages in Threads, Use External Emojis). Opening it for a server the bot is already in just
   updates its permissions.
3. In each server: **Server Settings → Roles**, drag the bot's role **above** every role it
   should hand out. Discord only lets a bot give roles below its own.

**Letting server admins sign in with Discord** (optional; without it only you, with the panel
password, can change server settings):

1. The panel must be reachable from the internet at an https address, such as through a
   Cloudflare tunnel.
2. Panel → **Settings** → **Discord sign-in**: type that address and save. The panel then shows
   a redirect URL.
3. Developer Portal → **OAuth2** → **Redirects** → add that URL. Copy the **Client Secret** from the
   same page into the panel and save.

The sign-in page then shows **Sign in with Discord**. People who sign in see only the servers
where they have the Manage Server permission. The bot checks that on Discord on every change, and
they never see the bot's token, logs or music controls.

## How Spotify works

Spotify doesn't give out audio, so the bot reads the song names from the link and plays the best
match from YouTube (the closest length, avoiding live/cover/remix versions). Without any keys it
reads Spotify's public embed page: songs, albums, artists' top songs, and the **first 100 songs**
of a playlist. For longer playlists, add a Spotify **Client ID/secret** in Settings. Since 2026
Spotify only lets accounts with **Premium** create those keys. Private playlists can't be read either way.

## When something stops working

- **Songs fail / "Sign in to confirm you're not a bot"**: first press **Update yt-dlp now** and
  restart the bot. If it still fails, upload a `cookies.txt` (Upkeep tab) exported from a
  browser signed in to YouTube, preferably with a spare account.
- **The bot is online but silent**: check that FFmpeg shows a version on the Upkeep tab.
- **"Discord rejected the bot token"**: reset the token in the Developer Portal and paste the new one.
- **Slash commands don't appear**: they register when the bot starts. Re-open Discord (Ctrl+R).

## Notes

- discord.py **2.7+** is required. Since March 2026 Discord only allows encrypted (DAVE) voice
  connections, and older bots can't join voice channels.
- YouTube needs a JavaScript runtime; `yt-dlp[deno]` installs Deno automatically. The Docker
  image supports amd64 and arm64 (64-bit Raspberry Pi OS), but not 32-bit ARM.
- Layout: `musicbot/bot` is the Discord bot (`python -m musicbot.bot` runs it without the panel),
  and `musicbot/panel` is the web panel, which runs the bot as a child process.
