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

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Определение состояний для ConversationHandler ---
(
    CHOOSE_ACTION,
    CHOOSE_SPLIT_MODE,
    AWAIT_SPLIT_ORDER,
    AWAIT_SPLIT_FILE,
    AWAIT_COMBINE_FILES,
    AWAIT_ASSEMBLY_COMMON,
    AWAIT_ASSEMBLY_UNIQUE,
    AWAITING_GROUP_ACTION_CHOICE,
) = range(8)

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
        "Выберите, что вы хотите сделать, или просто отправьте мне файл.",
        reply_markup=MAIN_KEYBOARD,
    )
    return CHOOSE_ACTION

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает в главное меню, отменяя текущее действие."""
    context.user_data.clear()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Действие отменено. Выберите, что вы хотите сделать:",
        reply_markup=MAIN_KEYBOARD,
    )
    return CHOOSE_ACTION

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершает операцию и возвращает в главное меню."""
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Чем еще могу помочь?",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END

# --- "УМНЫЙ" ОБРАБОТЧИК ПЕРЕСЛАННЫХ ФАЙЛОВ ---

async def direct_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа, если пользователь сразу отправляет файл(ы)."""
    # Делаем так, чтобы attachments всегда был списком, вне зависимости от того,
    # как был отправлен файл (напрямую, пересылкой, группой).
    raw_attachments = update.message.effective_attachment
    attachments = list(raw_attachments) if isinstance(raw_attachments, tuple) else [raw_attachments]
    
    # Проверяем, что все отправленные файлы - это PDF.
    if not attachments or not all(hasattr(doc, 'mime_type') and doc.mime_type == 'application/pdf' for doc in attachments):
        await update.message.reply_text("Пожалуйста, убедитесь, что все отправленные файлы имеют формат PDF.")
        # Возвращаемся в начальное состояние, не начиная диалог
        return CHOOSE_ACTION

    if len(attachments) == 1:
        context.user_data['file_to_process'] = attachments[0]
        filename = attachments[0].file_name.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("По одному листу", callback_data="split_single"), InlineKeyboardButton("По два листа", callback_data="split_double")],
            [InlineKeyboardButton("Указать свой порядок", callback_data="split_custom")],
            [InlineKeyboardButton("« Отмена", callback_data="main_menu")],
        ])
        await update.message.reply_text(f"Я получил файл `{filename}`.\nКак именно вы хотите его разбить?", reply_markup=keyboard, parse_mode='MarkdownV2')
        return CHOOSE_SPLIT_MODE
    else:
        context.user_data['files_to_process'] = attachments
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖇️ Объединить все в один файл", callback_data="group_combine")],
            [InlineKeyboardButton("🪓 Разбить каждый файл", callback_data="group_split")],
            [InlineKeyboardButton("« Отмена", callback_data="main_menu")],
        ])
        await update.message.reply_text(f"Я получил {len(attachments)} файлов. Что вы хотите с ними сделать?", reply_markup=keyboard)
        return AWAITING_GROUP_ACTION_CHOICE

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
        await query.edit_message_text("Хорошо. Отправьте мне сообщение с порядком разбивки.\n\n**Например, `3,3,4`** разобьет документ на три части: 3, 3 и 4 страницы.", parse_mode='MarkdownV2')
        return AWAIT_SPLIT_ORDER
    else:
        if 'file_to_process' in context.user_data:
            return await split_file_handler(update, context)
        await query.edit_message_text("Понял. Теперь просто отправьте мне PDF файл для разбивки.")
        return AWAIT_SPLIT_FILE

async def receive_split_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = update.message.text
    if not re.match(r'^\d+(\s*,\s*\d+)*$', order):
        await update.message.reply_text("Формат неверный. Используйте только цифры и запятые. Например: `3,3,4`", parse_mode='MarkdownV2')
        return AWAIT_SPLIT_ORDER
    context.user_data['custom_order'] = [int(x.strip()) for x in order.split(',')]
    if 'file_to_process' in context.user_data:
        return await split_file_handler(update, context)
    await update.message.reply_text(f'Порядок "{order}" принят. Теперь отправьте PDF файл.')
    return AWAIT_SPLIT_FILE

async def split_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    if update.callback_query: await update.callback_query.edit_message_text("Начинаю обработку...")
    else: await context.bot.send_message(chat_id=chat_id, text="Файл принят. Начинаю обработку...")
    try:
        doc_to_process = context.user_data.get('file_to_process') or (update.message and update.message.document)
        file = await context.bot.get_file(doc_to_process.file_id)
        file_bytes = await file.download_as_bytearray()
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        total_pages = pdf_doc.page_count
        ranges, mode = [], context.user_data.get('split_mode')
        
        if mode in ('split_single', 'group_split'): ranges = [[i] for i in range(total_pages)]
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
        
        base_name = os.path.splitext(doc_to_process.file_name)[0]
        for i, page_range in enumerate(ranges):
            new_doc = fitz.open()
            new_doc.insert_pdf(pdf_doc, from_page=page_range[0], to_page=page_range[-1])
            await context.bot.send_document(chat_id=chat_id, document=new_doc.write(), filename=f"{base_name}_part_{i + 1}.pdf")
            new_doc.close()
        pdf_doc.close()
        await context.bot.send_message(chat_id=chat_id, text="Готово! Все части файла отправлены.")
    except Exception as e:
        logger.error(f"Ошибка при разбивке: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Произошла ошибка. Возможно, файл поврежден.")
    return await end_conversation(update, context)

async def handle_group_action_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'group_combine': return await combine_files_handler(update, context)
    elif query.data == 'group_split':
        context.user_data['split_mode'] = 'group_split'
        await query.edit_message_text("Хорошо, начинаю разбивку каждого файла...")
        files_to_split = context.user_data.get('files_to_process', [])
        for doc in files_to_split:
            context.user_data['file_to_process'] = doc
            mock_update = type('MockUpdate', (), {'effective_chat': update.effective_chat, 'message': None, 'callback_query': None})()
            await split_file_handler(mock_update, context)
        return await end_conversation(update, context)

# --- ЛОГИКА СЦЕНАРИЯ "ОБЪЕДИНИТЬ PDF" ---
async def ask_for_combine_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Все файлы отправлены", callback_data="process_done")], [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]])
    await query.edit_message_text("Отправляйте PDF файлы для объединения. Когда закончите, нажмите кнопку.", reply_markup=keyboard)
    return AWAIT_COMBINE_FILES

async def receive_file_for_list(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int):
    if 'files_to_process' not in context.user_data: context.user_data['files_to_process'] = []
    context.user_data['files_to_process'].append(update.message.document)
    return next_state

async def combine_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    documents = context.user_data.get('files_to_process', [])
    if len(documents) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Нужно хотя бы два файла для объединения. Пришлите еще.")
        return AWAIT_COMBINE_FILES
    await query.edit_message_text("Начинаю объединение...")
    try:
        result_doc = fitz.open()
        for doc in documents:
            file = await context.bot.get_file(doc.file_id)
            file_bytes = await file.download_as_bytearray()
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            result_doc.insert_pdf(pdf_doc)
            pdf_doc.close()
        await context.bot.send_document(chat_id=update.effective_chat.id, document=result_doc.write(), filename="combined_document.pdf", caption="Готово! Ваш объединенный файл.")
        result_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при объединении: {e}")
        await query.message.reply_text("Произошла ошибка при обработке одного из файлов.")
    return await end_conversation(update, context)

# --- ЛОГИКА СЦЕНАРИЯ "СОБРАТЬ С ОБЩИМ ФАЙЛОМ" ---
async def ask_for_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отправьте ОДИН общий PDF файл, который будет добавлен ко всем остальным.")
    return AWAIT_ASSEMBLY_COMMON

async def receive_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.effective_attachment and len(update.message.effective_attachment) > 1:
        await update.message.reply_text("Пожалуйста, отправьте только ОДИН общий файл. Я возьму первый из присланных.")
    context.user_data['common_file'] = update.message.document
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Собрать файлы", callback_data="process_done")], [InlineKeyboardButton("« Назад", callback_data="main_menu")]])
    await update.message.reply_text("Общий файл принят. Теперь отправляйте уникальные PDF. Когда закончите, нажмите кнопку.", reply_markup=keyboard)
    return AWAIT_ASSEMBLY_UNIQUE

async def assembly_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    unique_docs = context.user_data.get('files_to_process', [])
    common_doc_msg = context.user_data.get('common_file')
    if not unique_docs:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Вы не отправили ни одного уникального файла для сборки. Пришлите хотя бы один.")
        return AWAIT_ASSEMBLY_UNIQUE
    await query.edit_message_text("Начинаю сборку...")
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
            await context.bot.send_document(chat_id=update.effective_chat.id, document=result_doc.write(), filename=f"assembled_{doc.file_name}")
            result_doc.close()
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Готово! Все файлы собраны и отправлены.")
    except Exception as e:
        logger.error(f"Ошибка при сборке: {e}")
        await query.message.reply_text("Произошла ошибка при обработке одного из файлов.")
    return await end_conversation(update, context)

# --- ОБРАБОТЧИКИ ОШИБОК И НЕВЕРНОГО ВВОДА ---
async def handle_invalid_file_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Это не PDF-файл. Я умею работать только с PDF.")
async def handle_unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сейчас я ожидаю файл, а не текст. Пожалуйста, отправьте документ.")
async def handle_unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не уверен, что с этим делать. Начните заново с /start.")

# --- ТОЧКА ВХОДА И ЗАПУСК ---
def main():
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN: raise ValueError("Необходимо установить переменную окружения TELEGRAM_TOKEN")
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.ATTACHMENT, direct_file_handler)],
        states={
            CHOOSE_ACTION: [
               CallbackQueryHandler(ask_split_mode, pattern="^split$"),
               CallbackQueryHandler(ask_for_combine_files, pattern="^combine$"),
               CallbackQueryHandler(ask_for_assembly_common_file, pattern="^assembly$"),
               # Добавляем наш "умный" обработчик сюда тоже
               MessageHandler(filters.ATTACHMENT, direct_file_handler)
            ],
            CHOOSE_SPLIT_MODE: [CallbackQueryHandler(handle_split_choice, pattern="^split_")],
            AWAIT_SPLIT_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split_order)],
            AWAIT_SPLIT_FILE: [MessageHandler(filters.Document.PDF, split_file_handler)],
            AWAIT_COMBINE_FILES: [MessageHandler(filters.Document.PDF, lambda u, c: receive_file_for_list(u, c, AWAIT_COMBINE_FILES)), CallbackQueryHandler(combine_files_handler, pattern="^process_done$")],
            AWAIT_ASSEMBLY_COMMON: [MessageHandler(filters.Document.PDF, receive_assembly_common_file)],
            AWAIT_ASSEMBLY_UNIQUE: [MessageHandler(filters.Document.PDF, lambda u, c: receive_file_for_list(u, c, AWAIT_ASSEMBLY_UNIQUE)), CallbackQueryHandler(assembly_files_handler, pattern="^process_done$")],
            AWAITING_GROUP_ACTION_CHOICE: [CallbackQueryHandler(handle_group_action_choice, pattern="^group_")],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            MessageHandler(filters.Document.ALL & ~filters.Document.PDF, handle_invalid_file_type),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_text),
            MessageHandler(filters.ALL, handle_unexpected_input),
        ],
        per_message=False,
    )
    application.add_handler(conv_handler)
    application.add_error_handler(lambda u, c: logger.error(f"Update {u} caused error {c.error}"))

    WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
    if WEBHOOK_URL:
        port = int(os.environ.get('PORT', '8443'))
        application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{WEBHOOK_URL}/{TOKEN}")
    else:
        application.run_polling()

if __name__ == "__main__":
    main()


