from telegram import Bot
from telegram.ext import Updater, CommandHandler

TOKEN = '17689462670:AAG_bROOj36O-zZ6B3uMcukUNUsWA0wtw4Y'

def start(update, context):
    update.message.reply_text('Hello! I am your spam bot.')

def send_message(update, context):
    chat_id = update.message.chat_id
    message = 'This is a spam message!'
    context.bot.send_message(chat_id=chat_id, text=message)

updater = Updater(TOKEN, use_context=True)
dp = updater.dispatcher

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("spam", send_message))

updater.start_polling()
updater.idle()