# Herbie the Holler Bot — Changelog

## v1.0 — Initial Release (2026-03-26)

### Core Bot
- Discord bot powered by Google Gemini, later switched to ElectronHub for chat
- Character-driven personality loaded from `herbie_character.json`
- Slash commands: `/activate`, `/deactivate`, `/start`, `/private`, `/memory`, `/settings`
- Reaction controls: 💫 to regenerate, 🗑️ to delete
- `!herbie` for help, `!delete #` for message cleanup, `!sync` for slash command sync
- Rate limiting to prevent spam

### AI & Models
- **Chat:** ElectronHub API with `gemini-2.5-flash` (primary) and `llama-4-maverick-17b-128e-instruct` (fallback)
- **Audio:** Google Gemini `gemini-3-flash-preview` for audio analysis
- Character actor system prompt with anti-repetition mandate
- Creative swearing and adult-server tone baked into personality

### Audio Analysis
- Listens to uploaded audio files (mp3, wav, ogg, flac, aac, etc.)
- Discord voice message detection and transcription
- Suno link detection — paste a suno.com link and Herbie listens to the track
- Gives music feedback in-character: genre, instruments, what's hitting, what could hit harder

### Memory
- MySQL persistent memory (hosted on fps.ms)
- Per-user per-channel conversation history
- Survives bot restarts
- `/start` clears local session only — MySQL history preserved
- 20-message context window sent to AI

### Reliability
- Per-user async locks to prevent message crossing on fast messages
- Fallback to channel.send if original message is deleted before reply
- Long message splitting for responses over 2000 characters
- Auto-fallback to secondary model if primary fails

### Infrastructure
- Hosted on fps.ms
- MySQL database on db0.fps.ms
- GitHub repo: https://github.com/at-the-studio/herbie
