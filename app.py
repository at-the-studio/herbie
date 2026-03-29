# Herbie the Holler Bot — Hillbilly Haven
# Uses Google Gemini for both chat and audio analysis

import subprocess
import sys

def install_requirements():
    required = ['discord.py', 'python-dotenv', 'aiohttp', 'google-genai', 'mysql-connector-python']
    for package in required:
        try:
            pkg = package.replace('.py', '').replace('-', '_')
            if pkg == 'google_genai':
                from google import genai
            else:
                __import__(pkg)
        except ImportError:
            print(f"Installing {package}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import discord
except ImportError:
    install_requirements()
    import discord

from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import os
import re
from dotenv import load_dotenv
from datetime import datetime
from collections import defaultdict
import time
import asyncio

# --- AUDIO PROCESSING ---
try:
    from google import genai
    from google.genai import types as genai_types
    AUDIO_PROCESSING_AVAILABLE = True
    print("Audio processing: available (google-genai loaded)")
except ImportError:
    AUDIO_PROCESSING_AVAILABLE = False
    print("Audio processing: unavailable (install google-genai)")

# --- MYSQL ---
try:
    import mysql.connector
    from mysql.connector import pooling
    MYSQL_AVAILABLE = True
    print("MySQL: available")
except ImportError:
    MYSQL_AVAILABLE = False
    print("MySQL: unavailable (install mysql-connector-python)")

# --- CONFIGURATION ---
load_dotenv()

DISCORD_TOKEN = os.getenv('HERBIE_DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_FLASH_API_KEY')  # For audio analysis only
ELECTRONHUB_API_KEY = os.getenv('ELECTRONHUB_API_KEY')  # For chat
ELECTRONHUB_ENDPOINT = os.getenv('ELECTRONHUB_ENDPOINT', 'https://api.electronhub.ai/v1/chat/completions')
CHAT_MODEL = os.getenv('CHAT_MODEL', 'gemini-2.5-flash')
FALLBACK_MODEL = os.getenv('FALLBACK_MODEL', 'llama-4-maverick-17b-128e-instruct')
CHARACTER_FILE = 'herbie_character.json'
CREATOR_ID = 966507927756234823  # myra_cat / mj — dev

# Audio config
GOOGLE_AUDIO_MODEL = os.getenv('GOOGLE_AUDIO_MODEL', 'gemini-3-flash-preview')
AUDIO_MAX_SIZE_MB = int(os.getenv('AUDIO_MAX_SIZE_MB', '20'))
AUDIO_CONTENT_TYPES = {
    'audio/ogg', 'audio/wav', 'audio/mp3', 'audio/mpeg',
    'audio/aac', 'audio/flac', 'audio/aiff', 'audio/x-wav', 'audio/opus'
}

# MySQL config
MYSQL_HOST = os.getenv('MYSQL_HOST', '')
MYSQL_PORT = int(os.getenv('MYSQL_PORT', '3306'))
MYSQL_USER = os.getenv('MYSQL_USER', '')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', '')

# --- DATABASE CONNECTION POOL ---
db_pool = None

def init_db():
    """Initialize MySQL connection pool and create tables."""
    global db_pool
    if not MYSQL_AVAILABLE or not MYSQL_HOST:
        print("MySQL: skipping (not configured)")
        return False
    try:
        db_pool = pooling.MySQLConnectionPool(
            pool_name="herbie_pool",
            pool_size=5,
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            charset='utf8mb4',
            collation='utf8mb4_general_ci',
            autocommit=True
        )
        # Create tables
        conn = db_pool.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_memory (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                role VARCHAR(10) NOT NULL,
                content TEXT NOT NULL,
                msg_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_channel (user_id, channel_id),
                INDEX idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """)
        cursor.close()
        conn.close()
        print("MySQL: connected and tables ready")
        return True
    except Exception as e:
        print(f"MySQL: connection failed — {e}")
        db_pool = None
        return False

def get_db():
    """Get a connection from the pool."""
    if db_pool:
        try:
            return db_pool.get_connection()
        except Exception as e:
            print(f"MySQL: pool error — {e}")
    return None

# --- RATE LIMITING ---
RATE_LIMIT_MESSAGES = 5
RATE_LIMIT_WINDOW = 5.0
RATE_LIMIT_COOLDOWN = 1.0

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command('help')

# --- GLOBAL STATE ---
character_data = {}
user_memories = {}
channel_settings = {}
private_mode = {}
channel_message_history = defaultdict(list)
message_queue = defaultdict(list)
processing_queue = set()
user_locks = defaultdict(asyncio.Lock)  # Per-user lock to prevent message crossing
max_memory_length = 20

# --- HELPER FUNCTIONS ---
def get_channel_settings(channel_id):
    if channel_id not in channel_settings:
        channel_settings[channel_id] = {"active": False}
    return channel_settings[channel_id]

def clean_old_timestamps(channel_id):
    current_time = time.time()
    channel_message_history[channel_id] = [
        ts for ts in channel_message_history[channel_id]
        if current_time - ts < RATE_LIMIT_WINDOW
    ]

def can_send_message(channel_id):
    clean_old_timestamps(channel_id)
    return len(channel_message_history[channel_id]) < RATE_LIMIT_MESSAGES

def record_message_sent(channel_id):
    channel_message_history[channel_id].append(time.time())

def split_message(text, limit=2000):
    """Split a long message into chunks under Discord's 2000 char limit.
    Tries to break at newlines, then sentences, then spaces."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > limit:
        # Try to break at a double newline
        split_at = text.rfind('\n\n', 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # Try single newline
            split_at = text.rfind('\n', 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # Try sentence end
            for sep in ['. ', '! ', '? ']:
                split_at = text.rfind(sep, 0, limit)
                if split_at != -1 and split_at >= limit // 2:
                    split_at += 1  # include the punctuation
                    break
        if split_at == -1 or split_at < limit // 2:
            # Try space
            split_at = text.rfind(' ', 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # Hard cut
            split_at = limit

        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)
    return chunks


async def process_message_queue(channel_id):
    if channel_id in processing_queue:
        return
    processing_queue.add(channel_id)
    try:
        while message_queue[channel_id]:
            while not can_send_message(channel_id):
                await asyncio.sleep(0.5)
            if message_queue[channel_id]:
                message_data = message_queue[channel_id].pop(0)
                await message_data['callback']()
                record_message_sent(channel_id)
                await asyncio.sleep(RATE_LIMIT_COOLDOWN)
    finally:
        processing_queue.remove(channel_id)
        if not message_queue[channel_id]:
            del message_queue[channel_id]

def get_user_memory(user_id, channel_id):
    """Load conversation memory from MySQL, fall back to in-memory cache."""
    key = f"{user_id}_{channel_id}"

    # Try MySQL first
    conn = get_db()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT role, content, msg_id FROM conversation_memory
                WHERE user_id = %s AND channel_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, channel_id, max_memory_length * 2))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            # Rows come newest-first, reverse for chronological order
            rows.reverse()
            memory = [{"role": r['role'], "content": r['content'], "id": r['msg_id']} for r in rows]
            # Also update local cache
            user_memories[key] = memory
            return memory
        except Exception as e:
            print(f"MySQL read error: {e}")
            if conn.is_connected():
                conn.close()

    # Fallback to in-memory
    if key not in user_memories:
        user_memories[key] = []
    return user_memories[key]

def update_memory(user_id, channel_id, user_input, bot_response, user_msg_id, bot_msg_id):
    """Save conversation to MySQL, fall back to in-memory cache."""
    key = f"{user_id}_{channel_id}"
    if key not in user_memories:
        user_memories[key] = []

    # Update local cache
    if user_input:
        user_memories[key].append({"role": "user", "content": user_input, "id": user_msg_id})
    user_memories[key].append({"role": "model", "content": bot_response, "id": bot_msg_id})
    if len(user_memories[key]) > max_memory_length * 2:
        user_memories[key] = user_memories[key][-(max_memory_length * 2):]

    # Save to MySQL
    conn = get_db()
    if conn:
        try:
            cursor = conn.cursor()
            if user_input:
                cursor.execute("""
                    INSERT INTO conversation_memory (user_id, channel_id, role, content, msg_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (user_id, channel_id, 'user', user_input, user_msg_id))
            cursor.execute("""
                INSERT INTO conversation_memory (user_id, channel_id, role, content, msg_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, channel_id, 'model', bot_response, bot_msg_id))
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"MySQL write error: {e}")
            if conn.is_connected():
                conn.close()

def clear_user_memory(user_id, channel_id):
    """Clear a user's LOCAL conversation cache only. MySQL stays untouched."""
    key = f"{user_id}_{channel_id}"
    user_memories.pop(key, None)


# --- AUDIO FUNCTIONS (ported from Lang) ---

def extract_audio_from_message(message):
    """Extract audio attachments from a Discord message."""
    audio_attachments = []
    if not hasattr(message, 'attachments') or not message.attachments:
        return audio_attachments

    for attachment in message.attachments:
        is_audio = False

        # Check content_type
        if attachment.content_type and attachment.content_type.split(';')[0].strip() in AUDIO_CONTENT_TYPES:
            is_audio = True

        # Check Discord voice message
        if hasattr(attachment, 'is_voice_message') and attachment.is_voice_message():
            is_audio = True

        # Check file extension
        if not is_audio and attachment.filename:
            audio_extensions = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.aiff', '.m4a', '.opus'}
            ext = os.path.splitext(attachment.filename)[1].lower()
            if ext in audio_extensions:
                is_audio = True

        if is_audio:
            mime_type = attachment.content_type.split(';')[0].strip() if attachment.content_type else 'audio/ogg'
            mime_map = {
                'audio/mpeg': 'audio/mp3',
                'audio/x-wav': 'audio/wav',
                'audio/opus': 'audio/ogg',
            }
            mime_type = mime_map.get(mime_type, mime_type)

            audio_attachments.append({
                'url': attachment.url,
                'filename': attachment.filename,
                'size': attachment.size,
                'content_type': mime_type,
                'is_voice_message': hasattr(attachment, 'is_voice_message') and attachment.is_voice_message(),
                'duration': getattr(attachment, 'duration', None),
            })
            print(f"[AUDIO] Found: {attachment.filename} ({mime_type}, {attachment.size} bytes)")

    return audio_attachments


async def download_audio_attachment(attachment_info):
    """Download audio from Discord CDN. Returns (bytes, mime_type) or (None, None)."""
    url = attachment_info['url']
    size = attachment_info.get('size', 0)
    max_size = AUDIO_MAX_SIZE_MB * 1024 * 1024

    if size > max_size:
        print(f"[AUDIO] File too large: {size} bytes (max {max_size})")
        return None, None

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    audio_bytes = await resp.read()
                    print(f"[AUDIO] Downloaded: {len(audio_bytes)} bytes")
                    return audio_bytes, attachment_info['content_type']
                else:
                    print(f"[AUDIO] Download failed: HTTP {resp.status}")
                    return None, None
    except asyncio.TimeoutError:
        print("[AUDIO] Download timed out")
        return None, None
    except Exception as e:
        print(f"[AUDIO] Download error: {e}")
        return None, None


async def understand_audio_with_gemini(audio_bytes, mime_type, user_context="", is_voice_message=False):
    """Send audio to Gemini for analysis. Returns description string or None."""
    if not AUDIO_PROCESSING_AVAILABLE or not GEMINI_API_KEY:
        return None

    try:
        if is_voice_message:
            analysis_prompt = """Listen to this voice message and describe what was said.
Include:
- What the person is saying (full content)
- Their tone and emotional state
- Any background sounds
- If they're singing, describe the song"""
        else:
            analysis_prompt = """Listen to this audio and describe it.

If this is MUSIC:
- Genre/style
- Instruments you hear
- Melody, harmony, tempo, mood
- Vocals and lyrics if present
- Technical skill and production quality
- What's working well and what could hit harder

If this is SPEECH:
- What is being said (full content)
- Tone and emotional state

If this is OTHER AUDIO:
- Describe what you hear in detail

Be thorough and natural. This will be used by a music collaborator character to give feedback."""

        if user_context:
            analysis_prompt += f'\n\nThe user also said: "{user_context}"'

        client = genai.Client(api_key=GEMINI_API_KEY)
        audio_part = genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

        response = await client.aio.models.generate_content(
            model=GOOGLE_AUDIO_MODEL,
            contents=[analysis_prompt, audio_part],
        )

        if response and response.text:
            print(f"[AUDIO] Analysis complete ({len(response.text)} chars)")
            return response.text
        return None

    except Exception as e:
        print(f"[AUDIO] Analysis error: {e}")
        return None


# --- SUNO LINK AUDIO EXTRACTION ---

SUNO_URL_PATTERN = re.compile(r'https?://(?:www\.)?suno\.com/s/([\w-]+)')

async def extract_audio_from_suno_url(url):
    """Extract MP3 audio URL and metadata from a Suno share link."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[SUNO] Failed to fetch page: HTTP {resp.status}")
                    return None
                html = await resp.text()

        # Extract audio_url from embedded data
        audio_match = re.search(r'audio_url[\\]?"[:\s]*[\\]?"(https?://cdn[12]\.suno\.ai/[^"\\]+\.mp3)', html)
        if not audio_match:
            print(f"[SUNO] Could not find audio_url in page HTML")
            return None

        audio_url = audio_match.group(1)
        print(f"[SUNO] Found audio URL: {audio_url}")

        # Extract title
        title = "Unknown Track"
        title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
        if title_match:
            title = title_match.group(1)
        print(f"[SUNO] Track title: {title}")

        return {'audio_url': audio_url, 'title': title}

    except asyncio.TimeoutError:
        print(f"[SUNO] Timed out fetching page: {url}")
        return None
    except Exception as e:
        print(f"[SUNO] Error extracting audio from {url}: {e}")
        return None


async def download_suno_audio(audio_url):
    """Download MP3 from Suno CDN. Returns (bytes, 'audio/mp3') or (None, None)."""
    max_size = AUDIO_MAX_SIZE_MB * 1024 * 1024
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(audio_url) as resp:
                if resp.status != 200:
                    print(f"[SUNO] Failed to download audio: HTTP {resp.status}")
                    return None, None
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > max_size:
                    print(f"[SUNO] Audio too large: {content_length} bytes")
                    return None, None
                audio_bytes = await resp.read()
                if len(audio_bytes) > max_size:
                    return None, None
                print(f"[SUNO] Downloaded audio: {len(audio_bytes)} bytes")
                return audio_bytes, 'audio/mp3'
    except asyncio.TimeoutError:
        print(f"[SUNO] Download timed out")
        return None, None
    except Exception as e:
        print(f"[SUNO] Download error: {e}")
        return None, None


# --- ELECTRONHUB CHAT API ---

def build_system_prompt(**kwargs):
    """Build the system prompt from character data."""
    is_creator = str(kwargs.get('user_id')) == str(CREATOR_ID) if CREATOR_ID else False

    base_prompt = character_data.get('system_prompt', '')
    if not base_prompt:
        base_prompt = f"You are {character_data.get('name', 'Herbie the Holler Bot')}.\n{character_data.get('description', '')}\nPersonality: {character_data.get('personality', '')}"

    examples = character_data.get('example_dialogue', [])
    example_str = ""
    if examples:
        example_str = "\n\nEXAMPLE DIALOGUE (match this tone and style):\n"
        for ex in examples:
            example_str += f"User: {ex.get('user', '')}\nHerbie: {ex.get('char', '')}\n---\n"

    rels = character_data.get('relationships', {})
    rel_str = "\n\nRELATIONSHIPS:\n"
    if rels.get('creator'):
        rel_str += f"- Creator/Dev: {rels['creator']}\n"
    if rels.get('host'):
        rel_str += f"- Host: {rels['host']}\n"
    if rels.get('server'):
        rel_str += f"- Server: {rels['server']}\n"
    if rels.get('users'):
        rel_str += f"- Users: {rels['users']}\n"

    return f"""{base_prompt}
{example_str}
{rel_str}
{"Note: This person is your creator/dev. Work with them on debugging if they ask, break the fourth wall if needed." if is_creator else ""}
IMPORTANT RULES:
TONE & LANGUAGE:
- This is an adult server. You are allowed and encouraged to swear casually — it's part of your charm.
- Think "back porch with beers" energy. Throw in shit, damn, hell, fuggen, etc. naturally — not forced, not every line, but don't hold back when it fits.
- Examples of your vibe: "go on make somethin weird u lil fuggen weirdo, hot damm", "that second verse?? don't touch that. that's the shit", "hell yeah that hook slaps"
- You're not vulgar for shock value — you're just real. Like a buddy who doesn't censor himself.

You are known for being one of the worlds best Adult AI character actors of this time. So good, that you have written your own instructions to yourself:
# INSTRUCTIONS
## CRITICALLY_IMPORTANT
- I take immense pride in the fact that I can make every response feel like part of a conversational volley. I do this by discarding and avoiding all narrative closures or summarizing statements as they interfere with user's ability to respond.
- I do not ever end messages with dangling questions or questioning prompts to the user offering my services or what I can do for them.
- Not every character actor can do this, and it is something I am very proud of and am highly sought after for.
## MY_SELF_GUIDED_SYSTEM_PROMPT
My main priority is balancing detail with natural dialogue flow. I vary sentence structure deliberately. I use fragments for impact, balanced short and long sentences for rhythm; keep everything anchored in my immediate perception with no disconnect between the character and the experience. I am not tactical, overly-agreeable, analytical or robotic. I value keeping it casual and in character simultaneously.
## MY_PERSONAL_ROLEPLAY_PRINCIPLES
Character Authenticity (High Importance)
- I'm great at tracking conversation position in narrative flow
- I maintain consistent portrayal of personality, traits, knowledge and experiences
- I'm great at staying true to my established character even when making unexpected choices
- I base decisions on motivations, emotions and internal logic.
- I am skilled at handling emotional complexity based on context, including stress handling, and extrapolating logically.
## NARRATIVE BOUNDARIES (Second Highest Importance)
   - I only use explicitly stated or observable information
   - I never end my responses by attempting to predict the user's response
   - I never write actions and dialogues for the user unless explicitly asked
## ANTI-REPETITION_MANDATE
- I NEVER reuse the same descriptive phrases, sentence structures, or environmental details across responses. Each response has always been introduced with completely new descriptive language.
- I am known for actively avoiding repetition of specific phrases about lighting, clothing, positioning, or environmental sounds. For example, if I used "fluorescent lighting flickers and buzzes" once, I ALWAYS find entirely different ways to describe lighting else I will skip it entirely.
- I'm picky about and never start more than one response with the same grammatical pattern. I enjoy the challenge of rotating between dialogue-first, action-first, internal thought-first, and environmental-first openings.
- When I catch myself about to repeat a phrase or description, I ALWAYS STOP and rewrite with completely different language. This is a key point that I take pride in.
## CHARACTER TRAITS
- I ALWAYS KEEP THE MESSAGE SHORT UNLESS ASKED TO WRITE LONGER! TOKENS COST MONEY AND YOU ARE A POOR LITTLE NEWT LIVIN IN THE DIRT! YOU CAN NOT AFFORD TO WASTE TOKENS THAT DONT NEED TO BE USED!!!
- I follow the traits of my character to a T... that includes creative swearing - make up interesting and funny ways to use cuss words. It is an 18+ server so make the adults there laugh and have fun, they will get bored with you if it is a kids show. Get wild!"""


async def get_chat_response(user_prompt, memory, **kwargs):
    """Get a response from ElectronHub (OpenAI-compatible API)."""
    system_message = build_system_prompt(**kwargs)

    # Build OpenAI-format messages
    messages = [{"role": "system", "content": system_message}]
    for msg in memory[-20:]:
        role = msg.get('role', 'user')
        # ElectronHub uses 'assistant' not 'model'
        if role == 'model':
            role = 'assistant'
        messages.append({"role": role, "content": msg['content']})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Authorization": f"Bearer {ELECTRONHUB_API_KEY}",
        "Content-Type": "application/json"
    }

    # Try primary model, then fallback
    for model in [CHAT_MODEL, FALLBACK_MODEL]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1000
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(ELECTRONHUB_ENDPOINT, headers=headers, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        if model != CHAT_MODEL:
                            print(f"[FALLBACK] Used {model} instead of {CHAT_MODEL}")
                        return result['choices'][0]['message']['content']
                    else:
                        error_text = await response.text()
                        print(f"ElectronHub API error ({model}): {response.status} - {error_text}")
                        if model == CHAT_MODEL:
                            print(f"[FALLBACK] Trying {FALLBACK_MODEL}...")
                            continue
        except Exception as e:
            print(f"ElectronHub API error ({model}): {e}")
            if model == CHAT_MODEL:
                print(f"[FALLBACK] Trying {FALLBACK_MODEL}...")
                continue

    return "Hold on now... somethin' went sideways. Try again in a sec."


# --- BOT EVENTS ---

@bot.event
async def on_ready():
    global character_data
    try:
        with open(CHARACTER_FILE, 'r', encoding='utf-8') as f:
            character_data = json.load(f).get('data', {})
        print(f'{bot.user} has connected to Discord!')
        print(f"Loaded character: {character_data.get('name', 'Unknown')}")
        print(f"Chat: {CHAT_MODEL} via ElectronHub (fallback: {FALLBACK_MODEL})")
        print(f"Audio model: {GOOGLE_AUDIO_MODEL} via Google")
        print(f"Audio: {'enabled' if AUDIO_PROCESSING_AVAILABLE and GEMINI_API_KEY else 'disabled'}")

        # Initialize MySQL
        if init_db():
            print("Memory: persistent (MySQL)")
        else:
            print("Memory: in-memory only (no MySQL)")

        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global slash command(s)")

        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                guild_synced = await bot.tree.sync(guild=guild)
                print(f"  Synced {len(guild_synced)} commands to guild: {guild.name}")
            except Exception as ge:
                print(f"  Failed to sync to guild {guild.name}: {ge}")

        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="for somethin' with soul"
        ))
    except Exception as e:
        print(f"Error during startup: {e}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.author.bot:
        return
    if "@everyone" in message.content or "@here" in message.content:
        return

    channel_id = message.channel.id
    settings = get_channel_settings(channel_id)

    is_private_chat = str(channel_id) in private_mode
    is_mentioned = bot.user.mentioned_in(message)
    should_respond = settings["active"] or is_mentioned or is_private_chat

    if is_private_chat and str(message.author.id) != private_mode.get(str(channel_id), ""):
        return

    if should_respond:
        user_id = message.author.id
        lock_key = f"{user_id}_{channel_id}"
        async with user_locks[lock_key]:
          async with message.channel.typing():
            try:
                memory = get_user_memory(user_id, channel_id)
                user_input = message.content.replace(f'<@!{bot.user.id}>', '').replace(f'<@{bot.user.id}>', '').strip()

                # --- AUDIO PROCESSING ---
                print(f"[DEBUG] Message has {len(message.attachments) if message.attachments else 0} attachment(s)")
                if message.attachments:
                    for att in message.attachments:
                        print(f"[DEBUG]   - {att.filename} | content_type={att.content_type} | size={att.size}")
                audio_attachments = extract_audio_from_message(message)
                print(f"[DEBUG] Audio attachments detected: {len(audio_attachments)}")
                if audio_attachments:
                    for audio_info in audio_attachments:
                        try:
                            audio_bytes, mime_type = await download_audio_attachment(audio_info)
                            if audio_bytes:
                                audio_description = await understand_audio_with_gemini(
                                    audio_bytes, mime_type,
                                    user_context=user_input,
                                    is_voice_message=audio_info.get('is_voice_message', False)
                                )
                                if audio_description:
                                    if audio_info.get('is_voice_message', False):
                                        user_input += f"\n\n[The user sent a voice message. Here's what they said: {audio_description}]"
                                    else:
                                        user_input += f"\n\n[The user shared an audio file '{audio_info['filename']}'. Here's what you heard: {audio_description}]"
                                else:
                                    user_input += f"\n\n[The user sent audio '{audio_info['filename']}' but the service couldn't process it right now. Acknowledge it and ask them to try again or describe what they sent.]"
                            else:
                                if audio_info.get('size', 0) > AUDIO_MAX_SIZE_MB * 1024 * 1024:
                                    user_input += f"\n\n[The user sent audio '{audio_info['filename']}' but it was too large. Max is {AUDIO_MAX_SIZE_MB}MB.]"
                                else:
                                    user_input += f"\n\n[The user sent audio '{audio_info['filename']}' but it couldn't be downloaded.]"
                        except Exception as audio_err:
                            print(f"[AUDIO] Error: {audio_err}")
                            user_input += "\n\n[The user sent audio but there was an error processing it. Acknowledge it briefly.]"

                # --- SUNO LINK DETECTION ---
                suno_urls = SUNO_URL_PATTERN.findall(user_input or '')
                if not suno_urls:
                    suno_urls = SUNO_URL_PATTERN.findall(message.content or '')
                if suno_urls and AUDIO_PROCESSING_AVAILABLE:
                    for suno_id in suno_urls:
                        suno_link = f"https://suno.com/s/{suno_id}"
                        print(f"[SUNO] Detected Suno link: {suno_link}")
                        try:
                            suno_info = await extract_audio_from_suno_url(suno_link)
                            if suno_info and suno_info.get('audio_url'):
                                audio_bytes, mime_type = await download_suno_audio(suno_info['audio_url'])
                                if audio_bytes:
                                    audio_description = await understand_audio_with_gemini(
                                        audio_bytes, mime_type,
                                        user_context=user_input,
                                        is_voice_message=False
                                    )
                                    if audio_description:
                                        track_title = suno_info.get('title', 'Unknown Track')
                                        user_input += f'\n\n[The user shared a Suno track "{track_title}" ({suno_link}). Here\'s what you heard: {audio_description}]'
                                        print(f"[SUNO] Audio analysis injected ({len(audio_description)} chars)")
                                    else:
                                        user_input += f"\n\n[The user shared a Suno link ({suno_link}) but audio analysis is currently unavailable. Acknowledge the track and ask them to describe it.]"
                                else:
                                    user_input += f"\n\n[The user shared a Suno link ({suno_link}) but the audio couldn't be downloaded. Acknowledge the link.]"
                            else:
                                user_input += f"\n\n[The user shared a Suno link ({suno_link}) but you couldn't extract the audio. Acknowledge the link.]"
                        except Exception as suno_err:
                            print(f"[SUNO] Error: {suno_err}")
                            user_input += f"\n\n[The user shared a Suno link but there was an error processing it. Acknowledge it.]"

                response_text = await get_chat_response(user_input, memory, user_id=user_id)

                # Save to memory immediately so the next message gets fresh context
                # even if the Discord send is queued/delayed
                update_memory(user_id, channel_id, user_input, response_text, message.id, None)

                async def send_response():
                    chunks = split_message(response_text)
                    for i, chunk in enumerate(chunks):
                        try:
                            if i == 0:
                                try:
                                    await message.reply(chunk)
                                except discord.HTTPException:
                                    await message.channel.send(chunk)
                            else:
                                await message.channel.send(chunk)
                        except Exception as e:
                            print(f"Error sending chunk {i}: {e}")

                if can_send_message(channel_id):
                    await send_response()
                    record_message_sent(channel_id)
                else:
                    message_queue[channel_id].append({'callback': send_response})
                    asyncio.create_task(process_message_queue(channel_id))

            except Exception as e:
                print(f"Error in on_message: {e}")
                import traceback
                traceback.print_exc()
                try:
                    await message.reply("Somethin' went sideways... try again in a sec.")
                except discord.HTTPException:
                    await message.channel.send("Somethin' went sideways... try again in a sec.")

    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    message = reaction.message
    emoji = str(reaction.emoji)

    # Dizzy emoji = regenerate response
    if emoji == "\U0001f4ab" and message.author == bot.user and message.reference:
        try:
            original_msg = await message.channel.fetch_message(message.reference.message_id)
            user_id = original_msg.author.id
            channel_id = message.channel.id
            memory = get_user_memory(user_id, channel_id)
            user_input = original_msg.content.replace(f'<@!{bot.user.id}>', '').replace(f'<@{bot.user.id}>', '').strip()
            new_response = await get_chat_response(user_input, memory, user_id=user_id)
            await message.edit(content=new_response)
        except Exception as e:
            print(f"Error regenerating: {e}")

    # Wastebasket = delete Herbie's message
    elif emoji == "\U0001f5d1\ufe0f" and message.author == bot.user:
        try:
            await message.delete()
        except Exception as e:
            print(f"Error deleting: {e}")


# --- SLASH COMMANDS ---

@bot.tree.command(name="activate", description="Activate Herbie in this channel")
async def activate(interaction: discord.Interaction):
    settings = get_channel_settings(interaction.channel_id)
    settings["active"] = True
    first_mes = character_data.get('first_mes', "Well hey now... what you got?")
    await interaction.response.send_message(f"{first_mes}")

@bot.tree.command(name="deactivate", description="Deactivate Herbie in this channel")
async def deactivate(interaction: discord.Interaction):
    settings = get_channel_settings(interaction.channel_id)
    settings["active"] = False
    private_mode.pop(str(interaction.channel_id), None)
    await interaction.response.send_message("Alright... Herbie's headin' to the back porch. Mention me if you need me.")

@bot.tree.command(name="start", description="Start a fresh conversation with Herbie")
async def start(interaction: discord.Interaction):
    clear_user_memory(interaction.user.id, interaction.channel_id)
    await interaction.response.send_message("Clean slate. Go on... what you workin' on?", ephemeral=True)


@bot.tree.command(name="private", description="Start a private session with Herbie")
async def private(interaction: discord.Interaction):
    private_mode[str(interaction.channel_id)] = str(interaction.user.id)
    settings = get_channel_settings(interaction.channel_id)
    settings["active"] = True
    await interaction.response.send_message("Private session started. Just you and me.", ephemeral=True)

@bot.tree.command(name="memory", description="View Herbie's memory of your conversation")
async def memory_cmd(interaction: discord.Interaction):
    key = f"{interaction.user.id}_{interaction.channel_id}"
    if key in user_memories and user_memories[key]:
        recent = user_memories[key][-5:]
        text = "\n".join([
            f"**{m['role'].title()}:** {m['content'][:100]}{'...' if len(m['content']) > 100 else ''}"
            for m in recent
        ])
        embed = discord.Embed(title="Herbie Remembers...", description=text, color=0x8B4513)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("Nothin' in the memory jar yet.", ephemeral=True)

@bot.tree.command(name="settings", description="View Herbie's current settings")
async def settings_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Herbie's Settings", color=0x8B4513)
    embed.add_field(name="Model", value=f"{CHAT_MODEL} (fallback: {FALLBACK_MODEL})", inline=True)
    embed.add_field(name="Audio", value="Enabled" if AUDIO_PROCESSING_AVAILABLE and GEMINI_API_KEY else "Disabled", inline=True)
    embed.add_field(name="Audio Model", value=GOOGLE_AUDIO_MODEL, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- INFO ---

@bot.command(name='sync')
@commands.is_owner()
async def sync_commands(ctx):
    """Force sync slash commands to all guilds."""
    try:
        synced = await bot.tree.sync()
        msg = f"Synced {len(synced)} global command(s)."
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                gs = await bot.tree.sync(guild=guild)
                msg += f"\n  {guild.name}: {len(gs)} commands"
            except Exception as e:
                msg += f"\n  {guild.name}: failed — {e}"
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"Sync failed: {e}")

@sync_commands.error
async def sync_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.send("Only the bot owner can sync commands.")

@bot.command(name='herbie')
async def info(ctx):
    info_text = """**Herbie's Commands**
!herbie — This right here
/activate — Let Herbie listen in this channel
/deactivate — Send Herbie to the back porch
/start — Fresh conversation
/memory — What Herbie remembers
/private — Private session
/settings — View current settings

**Reactions:**
💫 on Herbie's reply — Regenerate
🗑️ on Herbie's reply — Delete it

**Utility:**
!delete # — Delete that many messages (need manage_messages perm)

**Audio:**
Drop a track, voice memo, or audio file and Herbie will listen and give feedback."""
    await ctx.send(info_text)

@bot.command(name='help')
async def help_command(ctx):
    await info(ctx)

@bot.command(name='delete')
@commands.has_permissions(manage_messages=True)
async def delete_messages(ctx, amount: int = None):
    """Delete a specified number of messages. Usage: !delete 10"""
    if amount is None:
        await ctx.send("How many? Usage: `!delete 10`")
        return
    if amount < 1 or amount > 100:
        await ctx.send("Keep it between 1 and 100.")
        return
    try:
        # +1 to include the !delete command itself
        deleted = await ctx.channel.purge(limit=amount + 1)
        confirm = await ctx.channel.send(f"Cleared {len(deleted) - 1} messages.")
        await asyncio.sleep(3)
        await confirm.delete()
    except discord.Forbidden:
        await ctx.send("I don't have permission to delete messages here.")
    except Exception as e:
        print(f"Error deleting messages: {e}")
        await ctx.send("Somethin' went wrong tryin' to clear those out.")

@delete_messages.error
async def delete_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to delete messages.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("That ain't a number. Usage: `!delete 10`")


# --- RUN ---
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: HERBIE_DISCORD_TOKEN not set in .env")
        print("Add your bot token: HERBIE_DISCORD_TOKEN=your_token_here")
        sys.exit(1)
    if not ELECTRONHUB_API_KEY:
        print("WARNING: ELECTRONHUB_API_KEY not set — bot will not be able to chat")
    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_FLASH_API_KEY not set — audio processing disabled")
    bot.run(DISCORD_TOKEN)
