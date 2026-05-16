import os
import telebot
import fitz
from telebot import types
import tempfile
import shutil
import atexit
import re
import random
import threading
import pymongo
from bson.binary import Binary
import time
from datetime import datetime, timezone

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
MONGO_URI = os.environ.get('MONGO_URI')

if not MONGO_URI:
    print("CRITICAL WARNING: MONGO_URI environment variable is not set! Database functions will fail.")

INCOMING_BACKUP_GROUP_ID_STR = os.environ.get('INCOMING_BACKUP_GROUP_ID', '').strip()
OUTGOING_BACKUP_GROUP_ID_STR = os.environ.get('OUTGOING_BACKUP_GROUP_ID', '').strip()
STORAGE_DIR = tempfile.mkdtemp(prefix='pdf-processor-')
PDF_SAVE_GARBAGE_LEVEL = 3
MAX_BULK_QUEUE_SIZE = 50
FREE_PDF_WEEKLY_LIMIT = 2
WEEKLY_RESET_SECONDS = 604800
RANDOM_WATERMARK_PAGE_INTERVAL = 3
MIN_WATERMARK_TRANSPARENCY_PERCENT = 1
MIN_WATERMARK_OPACITY = MIN_WATERMARK_TRANSPARENCY_PERCENT / 100.0
TEXT_WATERMARK_HORIZONTAL_MARGIN_RATIO = 0.12
TEXT_WATERMARK_TOP_RATIO = 0.42
TEXT_WATERMARK_BOTTOM_RATIO = 0.58
TEXT_WATERMARK_DIAGONAL_ANGLE = 45
TEXT_WATERMARK_FONT_MIN = 18
TEXT_WATERMARK_FONT_MAX = 48
TEXT_WATERMARK_FONT_DIVISOR = 12
IMAGE_WATERMARK_WIDTH_RATIO = 0.4
IMAGE_WATERMARK_HEIGHT_RATIO = 0.22
MAX_IMAGE_SUFFIX_LENGTH = 10
TELEGRAM_PHOTO_DEFAULT_SUFFIX = ".jpg"
TEXT_WATERMARK_OPTIMAL_CHARACTER_COUNT = 40
TEXT_WATERMARK_COLOR = (0.75, 0.75, 0.75)
SUPPORTED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
IMAGE_MIME_SUFFIX_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

print(f"Token loaded: {bool(TOKEN)}")

try:
    ALLOWED_USERS = [int(uid.strip()) for uid in ALLOWED_USERS_STR.split(',') if uid.strip()]
except Exception as e:
    ALLOWED_USERS = []

try:
    INCOMING_BACKUP_GROUP_ID = int(INCOMING_BACKUP_GROUP_ID_STR) if INCOMING_BACKUP_GROUP_ID_STR else None
except Exception:
    INCOMING_BACKUP_GROUP_ID = None

try:
    OUTGOING_BACKUP_GROUP_ID = int(OUTGOING_BACKUP_GROUP_ID_STR) if OUTGOING_BACKUP_GROUP_ID_STR else None
except Exception:
    OUTGOING_BACKUP_GROUP_ID = None

bot = telebot.TeleBot(TOKEN)
user_states = {}
state_lock = threading.Lock()
user_locks = {}

# MongoDB Setup
if MONGO_URI:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client['pdf_bot_database']
    users_col = db['users']
    watermarks_col = db['watermarks']
else:
    mongo_client = None
    db = None
    users_col = None
    watermarks_col = None

def get_user_lock(user_id):
    with state_lock:
        if user_id not in user_locks:
            user_locks[user_id] = threading.Lock()
        return user_locks[user_id]

def initialize_database():
    if mongo_client:
        print("MongoDB Atlas configured.")

def get_user_plan_info(user_id):
    now = time.time()
    user = users_col.find_one({"user_id": user_id})

    if not user:
        user = {
            "user_id
