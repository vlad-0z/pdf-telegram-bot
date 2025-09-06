import os
import logging
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
import fitz  # PyMuPDF

# --- Настройка логирования для отладки на Render ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Определение состояний для ConversationHandler ---
CHOOSE_ACTION, CHOOSE_SPLIT_MODE, AWAIT_SPLIT_FILE, AWAIT_SPLIT_ORDER, \
AWAIT_COMBINE_FILES, AWAIT_ASSEMBLY_COMMON, AWAIT_ASSEMBLY_UNIQUE = range(7)

# --- Клавиатуры ---
MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🪓 Разбить PDF файл", callback_data="split")],
    [InlineKeyboardButton("🖇️ Объединить несколько PDF", callback_data="combine")],
    [InlineKeyboardButton("➕ Собрать с общим файлом", callback_data="assembly")],
])

# --- ОСНОВНЫЕ ФУНКЦИИ ДИАЛОГА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога, сброс состояния и показ главного меню."""
    context.user_data.clear()
    await update.message.reply_text(
        "Здравствуйте! Я ваш помощник для работы с PDF.\n\n"
        "Выберите, что вы хотите сделать:",
        reply_markup=MAIN_KEYBOARD
    )
    return CHOOSE_ACTION

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает в главное меню."""
    context.user_data.clear()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Выберите, что вы хотите сделать:",
        reply_markup=MAIN_KEYBOARD
    )
    return CHOOSE_ACTION

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str = "Чем еще могу помочь?"):
    """Завершает операцию и возвращает в главное меню."""
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=message,
        reply_markup=MAIN_KEYBOARD
    )
    return CHOOSE_ACTION

# --- ЛОГИКА СЦЕНАРИЯ "РАЗБИТЬ PDF" ---

async def ask_split_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("По одному листу", callback_data="split_single"), InlineKeyboardButton("По два листа", callback_data="split_double")],
        [InlineKeyboardButton("Указать свой порядок", callback_data="split_custom")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")],
    ])
    await query.edit_message_text("Отлично! Как именно вы хотите разбить PDF файл?", reply_markup=keyboard)
    return CHOOSE_SPLIT_MODE

async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['split_mode'] = query.data
    if query.data == 'split_custom':
        await query.edit_message_text(
            "Хорошо. Пожалуйста, отправьте мне сообщение с порядком разбивки.\n\n"
            "**Например, если у вас 10-страничный документ и вы отправите `3,3,4`**, вы получите три файла:\n"
            "• **Первый:** со страницами 1-3\n• **Второй:** со страницами 4-6\n• **Третий:** со страницами 7-10\n\n"
            "Просто отправьте мне числа через запятую."
        )
        return AWAIT_SPLIT_ORDER
    else:
        await query.edit_message_text("Понял. Теперь просто отправьте мне PDF файл для разбивки.")
        return AWAIT_SPLIT_FILE

async def receive_split_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = update.message.text
    if not re.match(r'^\d+(,\d+)*$', order):
        await update.message.reply_text("Кажется, формат неверный. Используйте только цифры и запятые. Например: `3,3,4`")
        return AWAIT_SPLIT_ORDER
    context.user_data['custom_order'] = [int(x) for x in order.split(',')]
    await update.message.reply_text(f'Отлично, порядок "{order}" принят. Теперь отправьте PDF файл.')
    return AWAIT_SPLIT_FILE

async def split_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Файл принят. Начинаю обработку...")
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        total_pages = pdf_doc.page_count
        ranges, mode = [], context.user_data.get('split_mode')
        if mode == 'split_single': ranges = [[i] for i in range(total_pages)]
        elif mode == 'split_double':
            ranges = [[i, i + 1] for i in range(0, total_pages, 2)]
            if total_pages % 2 != 0: ranges[-1] = [total_pages - 1]
        elif mode == 'split_custom':
            order, current_page = context.user_data.get('custom_order', []), 0
            for part_size in order:
                if current_page >= total_pages: break
                end_page = min(current_page + part_size, total_pages)
                ranges.append(list(range(current_page, end_page)))
                current_page = end_page
        base_name = os.path.splitext(update.message.document.file_name)[0]
        for i, page_range in enumerate(ranges):
            new_doc = fitz.open()
            new_doc.insert_pdf(pdf_doc, from_page=page_range[0], to_page=page_range[-1])
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=new_doc.write(),
                filename=f"{base_name}_part_{i + 1}.pdf"
            )
            new_doc.close()
        await update.message.reply_text("Готово! Все части файла отправлены.")
    except Exception as e:
        logger.error(f"Ошибка при разбивке PDF: {e}")
        await update.message.reply_text("К сожалению, при обработке файла произошла ошибка. Возможно, он поврежден.")
    return await end_conversation(update, context)

# --- ЛОГИКА СЦЕНАРИЯ "ОБЪЕДИНИТЬ PDF" ---

async def ask_for_combine_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Все файлы отправлены", callback_data="process_done")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]
    ])
    await query.edit_message_text(
        "Понял. Отправляйте мне PDF файлы для объединения. Когда закончите, нажмите кнопку ниже.", reply_markup=keyboard)
    return AWAIT_COMBINE_FILES

async def receive_file_for_list(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int):
    """Общая функция для сбора нескольких файлов в список."""
    if 'files_to_process' not in context.user_data:
        context.user_data['files_to_process'] = []
    context.user_data['files_to_process'].append(update.message.document)
    return next_state

async def combine_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    documents = context.user_data.get('files_to_process', [])
    if len(documents) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Вы отправили только {len(documents)} файл. Нужно хотя бы два.")
        return AWAIT_COMBINE_FILES
    await query.edit_message_text("Отлично! Начинаю объединение...")
    try:
        result_doc = fitz.open()
        for doc in documents:
            file = await context.bot.get_file(doc.file_id)
            file_bytes = await file.download_as_bytearray()
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            result_doc.insert_pdf(pdf_doc)
            pdf_doc.close()
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=result_doc.write(),
            filename="combined_document.pdf",
            caption="Готово! Ваш объединенный файл."
        )
        result_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при объединении PDF: {e}")
        await query.message.reply_text("К сожалению, при обработке одного из файлов произошла ошибка.")
    return await end_conversation(update, context)

# --- ЛОГИКА СЦЕНАРИЯ "СОБРАТЬ С ОБЩИМ ФАЙЛОМ" ---

async def ask_for_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Хорошо. Сначала отправьте мне ОДИН общий PDF файл, который будет добавлен ко всем остальным.")
    return AWAIT_ASSEMBLY_COMMON

async def receive_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['common_file'] = update.message.document
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Собрать файлы", callback_data="process_done")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]
    ])
    await update.message.reply_text(
        "Общий файл принят. Теперь отправляйте УНИКАЛЬНЫЕ PDF файлы. Когда закончите, нажмите кнопку ниже.",
        reply_markup=keyboard
    )
    return AWAIT_ASSEMBLY_UNIQUE

async def assembly_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    unique_docs = context.user_data.get('files_to_process', [])
    common_doc_msg = context.user_data.get('common_file')
    if not unique_docs:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Вы не отправили ни одного уникального файла для сборки.")
        return AWAIT_ASSEMBLY_UNIQUE
    await query.edit_message_text("Все файлы получены. Начинаю сборку...")
    try:
        common_file = await context.bot.get_file(common_doc_msg.file_id)
        common_file_bytes = await common_file.download_as_bytearray()
        
        for doc in unique_docs:
            unique_file = await context.bot.get_file(doc.file_id)
            unique_file_bytes = await unique_file.download_as_bytearray()
            
            result_doc = fitz.open(stream=unique_file_bytes, filetype="pdf")
            common_pdf = fitz.open(stream=common_file_bytes, filetype="pdf")
            result_doc.insert_pdf(common_pdf)
            common_pdf.close()
            
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=result_doc.write(),
                filename=f"assembled_{doc.file_name}"
            )
            result_doc.close()
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Готово! Все файлы собраны и отправлены.")
    except Exception as e:
        logger.error(f"Ошибка при сборке PDF: {e}")
        await query.message.reply_text("К сожалению, при обработке одного из файлов произошла ошибка.")
    return await end_conversation(update, context)

# --- ОБРАБОТЧИКИ ОШИБОК И НЕВЕРНОГО ВВОДА ---

async def handle_invalid_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ой, это не PDF-файл. Пожалуйста, отправьте мне документ именно в формате PDF.")

async def handle_text_instead_of_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сейчас я ожидаю от вас PDF-файл. Пожалуйста, отправьте мне документ для обработки.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# --- ТОЧКА ВХОДА И ЗАПУСК ---

def main():
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN: raise ValueError("Необходимо установить переменную окружения TELEGRAM_TOKEN")
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_ACTION: [
                CallbackQueryHandler(ask_split_mode, pattern="^split$"),
                CallbackQueryHandler(ask_for_combine_files, pattern="^combine$"),
                CallbackQueryHandler(ask_for_assembly_common_file, pattern="^assembly$"),
            ],
            CHOOSE_SPLIT_MODE: [CallbackQueryHandler(handle_split_choice, pattern="^split_(single|double|custom)$")],
            AWAIT_SPLIT_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split_order)],
            AWAIT_SPLIT_FILE: [MessageHandler(filters.Document.PDF, split_file_handler)],
            AWAIT_COMBINE_FILES: [
                MessageHandler(filters.Document.PDF, lambda u, c: receive_file_for_list(u, c, AWAIT_COMBINE_FILES)),
                CallbackQueryHandler(combine_files_handler, pattern="^process_done$"),
            ],
            AWAIT_ASSEMBLY_COMMON: [MessageHandler(filters.Document.PDF, receive_assembly_common_file)],
            AWAIT_ASSEMBLY_UNIQUE: [
                MessageHandler(filters.Document.PDF, lambda u, c: receive_file_for_list(u, c, AWAIT_ASSEMBLY_UNIQUE)),
                CallbackQueryHandler(assembly_files_handler, pattern="^process_done$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            MessageHandler(filters.TEXT, handle_text_instead_of_file),
            MessageHandler(filters.ALL, handle_invalid_file),
        ],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
    if WEBHOOK_URL:
        port = int(os.environ.get('PORT', '8443'))
        application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{WEBHOOK_URL}/{TOKEN}")
    else:
        logger.info("Запуск в режиме polling...")
        application.run_polling()

if __name__ == "__main__":
    main()