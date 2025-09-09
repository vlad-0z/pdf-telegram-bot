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
from collections import defaultdict

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Состояния ---
CHOOSE_ACTION, CHOOSE_SPLIT_MODE, AWAIT_SPLIT_FILE, AWAIT_SPLIT_ORDER, \
AWAIT_COMBINE_FILES, AWAIT_ASSEMBLY_COMMON, AWAIT_ASSEMBLY_UNIQUE, CHOOSE_GROUP_ACTION = range(8)

# --- Вспомогательная функция для Markdown ---
def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- Клавиатуры ---
MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🪓 Разбить PDF файл", callback_data="split")],
    [InlineKeyboardButton("🖇️ Объединить несколько PDF", callback_data="combine")],
    [InlineKeyboardButton("➕ Собрать с общим файлом", callback_data="assembly")],
])

SPLIT_MODE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("По одному листу", callback_data="split_single"), InlineKeyboardButton("По два листа", callback_data="split_double")],
    [InlineKeyboardButton("Указать свой порядок", callback_data="split_custom")],
    [InlineKeyboardButton("« Отмена", callback_data="main_menu")],
])

# --- НОВИНКА: Клавиатура для групповых быстрых сценариев ---
GROUP_ACTION_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🖇️ Объединить все в один файл", callback_data="group_combine")],
    [InlineKeyboardButton("« Отмена", callback_data="main_menu")],
])

# --- Глобальный словарь для сбора медиагрупп ---
media_group_files = defaultdict(list)

# --- ОСНОВНЫЕ ФУНКЦИИ ДИАЛОГА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Здравствуйте! Я ваш помощник для работы с PDF.\n\nВыберите, что вы хотите сделать:",
        reply_markup=MAIN_KEYBOARD
    )
    return CHOOSE_ACTION

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Выберите, что вы хотите сделать:", reply_markup=MAIN_KEYBOARD)
    return CHOOSE_ACTION

async def return_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str) -> int:
    context.user_data.clear()
    chat_id = update.effective_chat.id
    if update.callback_query:
        await update.callback_query.answer()
    
    await context.bot.send_message(chat_id=chat_id, text=message)
    await context.bot.send_message(chat_id=chat_id, text="Чем еще могу помочь?", reply_markup=MAIN_KEYBOARD)
    return CHOOSE_ACTION

# --- УЛУЧШЕННАЯ ЛОГИКА ОБРАБОТКИ ФАЙЛОВ ---

async def process_media_group(context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает собранную группу файлов после задержки."""
    job_data = context.job.data
    media_group_id = job_data['media_group_id']
    chat_id = job_data['chat_id']
    
    documents = media_group_files.pop(media_group_id, [])
    if not documents: return

    # Проверяем, ждем ли мы файлы для какой-то операции
    expected_action = context.user_data.get('awaiting_file_for')

    if expected_action in ['combine', 'assembly_unique']:
        if 'files_to_process' not in context.user_data:
            context.user_data['files_to_process'] = []
        context.user_data['files_to_process'].extend(documents)
        await context.bot.send_message(chat_id, f"Добавлено {len(documents)} файла(ов) в список.")
    else:
        # Если файлы присланы "из ниоткуда" - это быстрый сценарий для группы
        context.user_data['group_files_to_process'] = documents
        await context.bot.send_message(
            chat_id,
            f"Я получила {len(documents)} файла(ов). Что с ними сделать?",
            reply_markup=GROUP_ACTION_KEYBOARD
        )

async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главный роутер для всех документов. Разделяет одиночные файлы и группы."""
    # --- СЦЕНАРИЙ 1: ФАЙЛ ЯВЛЯЕТСЯ ЧАСТЬЮ ГРУППЫ ---
    if update.message.media_group_id:
        media_group_id = update.message.media_group_id
        media_group_files[media_group_id].append(update.message.document)

        # Удаляем старый таймер и ставим новый, чтобы дождаться всех файлов
        jobs = context.job_queue.get_jobs_by_name(str(media_group_id))
        for job in jobs:
            job.schedule_removal()
        
        context.job_queue.run_once(
            process_media_group,
            when=1.5, # Даем 1.5 секунды на сбор группы
            data={'media_group_id': media_group_id, 'chat_id': update.effective_chat.id},
            name=str(media_group_id)
        )
        # Не возвращаем состояние, т.к. обработка отложена
        return

    # --- СЦЕНАРИЙ 2: ПРИШЕЛ ОДИНОЧНЫЙ ФАЙЛ ---
    else:
        expected_action = context.user_data.get('awaiting_file_for')
        
        # Если мы ждали этот файл в рамках диалога
        if expected_action == 'split':
            return await split_file_handler(update, context)
        if expected_action == 'combine':
            return await receive_file_for_list(update, context, AWAIT_COMBINE_FILES)
        if expected_action == 'assembly_common':
            return await receive_assembly_common_file(update, context)
        if expected_action == 'assembly_unique':
            return await receive_file_for_list(update, context, AWAIT_ASSEMBLY_UNIQUE)
        
        # Если файл пришел "из ниоткуда" (быстрый сценарий для одного файла)
        else:
            document = update.message.document
            if document.mime_type != 'application/pdf':
                await update.message.reply_text("Это не PDF-файл.")
                return CHOOSE_ACTION

            context.user_data['file_to_split'] = document
            safe_filename = escape_markdown_v2(document.file_name)
            await update.message.reply_text(
                f"Я получила файл `{safe_filename}`\nКак именно вы хотите его разбить?",
                reply_markup=SPLIT_MODE_KEYBOARD,
                parse_mode='MarkdownV2'
            )
            return CHOOSE_SPLIT_MODE

# --- ЛОГИКА СЦЕНАРИЯ "РАЗБИТЬ PDF" ---
# ... (Этот блок почти без изменений) ...
async def ask_split_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отлично! Как именно вы хотите разбить PDF файл?", reply_markup=SPLIT_MODE_KEYBOARD)
    return CHOOSE_SPLIT_MODE

async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['split_mode'] = query.data

    if 'file_to_split' in context.user_data:
        if query.data != 'split_custom':
            await query.edit_message_text("Поняла. Начинаю обработку...")
            return await split_file_handler(update, context, pre_saved=True)
        else:
            await query.edit_message_text("Хорошо. Отправьте порядок разбивки (например: `3,3,4`)", parse_mode="MarkdownV2")
            return AWAIT_SPLIT_ORDER
    else:
        if query.data == 'split_custom':
            await query.edit_message_text("Хорошо. Отправьте порядок разбивки (например: `3,3,4`)", parse_mode="MarkdownV2")
            return AWAIT_SPLIT_ORDER
        else:
            await query.edit_message_text("Поняла. Теперь просто отправьте мне PDF файл для разбивки.")
            context.user_data['awaiting_file_for'] = 'split'
            return AWAIT_SPLIT_FILE

async def receive_split_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = update.message.text
    if not re.match(r'^\d+(,\s*\d+)*$', order):
        await update.message.reply_text("Формат неверный. Используйте только цифры и запятые. Например: `3,3,4`", parse_mode="MarkdownV2")
        return AWAIT_SPLIT_ORDER
        
    context.user_data['custom_order'] = [int(x) for x in order.split(',')]
    
    if 'file_to_split' in context.user_data:
        await update.message.reply_text("Порядок принят. Начинаю обработку...")
        return await split_file_handler(update, context, pre_saved=True)
    else:
        await update.message.reply_text(f'Отлично, порядок "{order}" принят. Теперь отправьте PDF файл.')
        context.user_data['awaiting_file_for'] = 'split'
        return AWAIT_SPLIT_FILE

async def split_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, pre_saved: bool = False) -> int:
    if pre_saved:
        document = context.user_data.get('file_to_split')
    else:
        document = update.message.document
        await update.message.reply_text("Файл принят. Начинаю обработку...")

    final_message = "Готово! Все части файла отправлены."
    try:
        # ... логика разбивки ...
        file = await context.bot.get_file(document.file_id)
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
        
        base_name = os.path.splitext(document.file_name)[0]
        for i, page_range in enumerate(ranges):
            new_doc = fitz.open()
            new_doc.insert_pdf(pdf_doc, from_page=page_range[0], to_page=page_range[-1])
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=new_doc.write(),
                filename=f"{base_name}_part_{i + 1}.pdf"
            )
            new_doc.close()
        pdf_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при разбивке PDF: {e}")
        final_message = "К сожалению, при обработке файла произошла ошибка."
    
    return await return_to_main_menu(update, context, message=final_message)


# --- ЛОГИКА ОСТАЛЬНЫХ СЦЕНАРИЕВ ---

async def ask_for_combine_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Все файлы отправлены", callback_data="process_done")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]
    ])
    await query.edit_message_text(
        "Поняла. Отправляйте мне PDF файлы для объединения. Когда закончите, нажмите кнопку.", reply_markup=keyboard)
    context.user_data['awaiting_file_for'] = 'combine'
    return AWAIT_COMBINE_FILES

async def receive_file_for_list(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int):
    # Эта функция теперь только для одиночных файлов
    if 'files_to_process' not in context.user_data:
        context.user_data['files_to_process'] = []
    
    document = update.message.document
    context.user_data['files_to_process'].append(document)
        
    await update.message.reply_text(f"Файл '{document.file_name}' добавлен ({len(context.user_data['files_to_process'])} всего).")
    return next_state

async def combine_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, from_group: bool = False) -> int:
    query = update.callback_query
    await query.answer()
    
    documents = context.user_data.get('group_files_to_process') if from_group else context.user_data.get('files_to_process', [])
    
    if len(documents) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Нужно хотя бы два файла.")
        return CHOOSE_ACTION if from_group else AWAIT_COMBINE_FILES
    
    await query.edit_message_text("Отлично! Начинаю объединение...")
    final_message = "Готово! Ваш объединенный файл."
    try:
        # ... (логика объединения без изменений)
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
            filename="combined_document.pdf"
        )
        result_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при объединении PDF: {e}")
        final_message = "К сожалению, при обработке одного из файлов произошла ошибка."
        
    return await return_to_main_menu(update, context, message=final_message)
# ... Остальные функции (сборка с общим) остаются с аналогичной логикой ...
async def ask_for_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Хорошо. Сначала отправьте мне ОДИН общий PDF файл.")
    context.user_data['awaiting_file_for'] = 'assembly_common'
    return AWAIT_ASSEMBLY_COMMON

async def receive_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['common_file'] = update.message.document; context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Собрать файлы", callback_data="process_done")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]
    ])
    await update.message.reply_text("Общий файл принят. Теперь отправляйте УНИКАЛЬНЫЕ PDF файлы.", reply_markup=keyboard)
    context.user_data['awaiting_file_for'] = 'assembly_unique'
    return AWAIT_ASSEMBLY_UNIQUE

async def assembly_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    unique_docs = context.user_data.get('files_to_process', [])
    common_doc_msg = context.user_data.get('common_file')
    if not unique_docs:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Вы не отправили ни одного уникального файла.")
        context.user_data['awaiting_file_for'] = 'assembly_unique'
        return AWAIT_ASSEMBLY_UNIQUE
        
    await query.edit_message_text("Все файлы получены. Начинаю сборку...")
    final_message = "Готово! Все файлы собраны и отправлены."
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
    except Exception as e:
        logger.error(f"Ошибка при сборке PDF: {e}")
        final_message = "К сожалению, при обработке одного из файлов произошла ошибка."

    return await return_to_main_menu(update, context, message=final_message)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)

def main():
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN: raise ValueError("Необходимо установить переменную окружения TELEGRAM_TOKEN")
    application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.PDF & filters.ChatType.PRIVATE, document_router),
            # Обработчик для кнопки "Объединить" из быстрого сценария
            CallbackQueryHandler(lambda u, c: combine_files_handler(u, c, from_group=True), pattern="^group_combine$"),
        ],
        states={
            CHOOSE_ACTION: [
                CallbackQueryHandler(ask_split_mode, pattern="^split$"),
                CallbackQueryHandler(ask_for_combine_files, pattern="^combine$"),
                CallbackQueryHandler(ask_for_assembly_common_file, pattern="^assembly$"),
            ],
            CHOOSE_SPLIT_MODE: [CallbackQueryHandler(handle_split_choice, pattern="^split_(single|double|custom)$")],
            AWAIT_SPLIT_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_split_order)],
            AWAIT_SPLIT_FILE: [MessageHandler(filters.Document.PDF, document_router)],
            AWAIT_COMBINE_FILES: [
                MessageHandler(filters.Document.PDF, document_router),
                CallbackQueryHandler(combine_files_handler, pattern="^process_done$"),
            ],
            AWAIT_ASSEMBLY_COMMON: [MessageHandler(filters.Document.PDF, document_router)],
            AWAIT_ASSEMBLY_UNIQUE: [
                MessageHandler(filters.Document.PDF, document_router),
                CallbackQueryHandler(assembly_files_handler, pattern="^process_done$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu, pattern="^main_menu$"),
        ],
        allow_reentry=True
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
