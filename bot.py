import os
import telebot

# Environment variables walin token ekai user IDs tikai gannawa
TOKEN = os.environ.get('BOT_TOKEN')
# Oyai yaluwo dennai ge Telegram User IDs comma eken wen karala Heroku ekata denna oni
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
ALLOWED_USERS = [int(uid.strip()) for uid in ALLOWED_USERS_STR.split(',') if uid.strip()]

bot = telebot.TeleBot(TOKEN)
user_states = {} # Users lage file progress eka track karanna

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id not in ALLOWED_USERS:
        bot.reply_to(message, "Sorry, you are not authorized to use this bot.")
        return
    bot.reply_to(message, "Send me a PDF file to rename.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    
    # Authorized user kenek da kiyala check karanawa
    if user_id not in ALLOWED_USERS:
        bot.reply_to(message, "Sorry, you are not authorized to use this bot.")
        return
    
    # PDF ekakda kiyala check karanawa
    if message.document.mime_type != 'application/pdf' and not message.document.file_name.lower().endswith('.pdf'):
        bot.reply_to(message, "Please send a valid PDF file.")
        return

    # File ID eka save karagannawa
    user_states[user_id] = {'file_id': message.document.file_id}
    bot.reply_to(message, "Please send the new name for this PDF file.")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    
    if user_id not in ALLOWED_USERS:
        return
        
    if user_id in user_states and 'file_id' in user_states[user_id]:
        new_name = message.text
        # Name eke .pdf thiyenawada check karala nattam add karanawa
        if not new_name.lower().endswith('.pdf'):
            new_name += '.pdf'
            
        bot.reply_to(message, "Renaming and uploading...")
        
        try:
            # File eka download karanawa
            file_info = bot.get_file(user_states[user_id]['file_id'])
            downloaded_file = bot.download_file(file_info.file_path)
            
            # Aluth namata save karanawa
            with open(new_name, 'wb') as new_file:
                new_file.write(downloaded_file)
                
            # Rename karapu file eka yawanawa
            with open(new_name, 'rb') as final_file:
                bot.send_document(user_id, final_file)
                
            # Heroku server eken file eka delete karanawa (space full wena eka nawaththanna)
            os.remove(new_name)
            
            # State eka clear karanawa
            del user_states[user_id]
            
        except Exception as e:
            bot.reply_to(message, f"An error occurred: {str(e)}")
            if user_id in user_states:
                del user_states[user_id]
    else:
        bot.reply_to(message, "Please send a PDF file first.")

print("Bot is running...")
bot.polling()
