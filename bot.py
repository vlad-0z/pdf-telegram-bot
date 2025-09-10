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
AWAIT_COMBINE_FILES, AWAIT_ASSEMBLY_COMMON, AWAIT_ASSEMBLY_UNIQUE, \
AWAIT_PDF_TO_IMAGE_FILE, AWAIT_PAGE_RANGE_FOR_IMAGE = range(9)


# --- Вспомогательная функция для Markdown ---
def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- Клавиатуры ---
MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🪓 Разбить PDF файл", callback_data="split")],
    [InlineKeyboardButton("🖇️ Объединить несколько PDF", callback_data="combine")],
    [InlineKeyboardButton("➕ Собрать с общим файлом", callback_data="assembly")],
    [InlineKeyboardButton("📄 PDF в Картинки", callback_data="pdf_to_img")],
])

SPLIT_MODE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("По одному листу", callback_data="split_single"), InlineKeyboardButton("По два листа", callback_data="split_double")],
    [InlineKeyboardButton("Указать свой порядок", callback_data="split_custom")],
    [InlineKeyboardButton("« Отмена", callback_data="main_menu")],
])

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

# --- ЛОГИКА ОБРАБОТКИ ФАЙЛОВ ---
async def process_media_group(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; media_group_id, chat_id, user_id, action = job_data['media_group_id'], job_data['chat_id'], job_data['user_id'], job_data['action']
    documents = media_group_files.pop(media_group_id, [])
    if not documents: return
    user_data = context.application.user_data.get(user_id, {})
    if action in ['combine', 'assembly_unique']:
        if 'files_to_process' not in user_data: user_data['files_to_process'] = []
        user_data['files_to_process'].extend(documents)
        await context.bot.send_message(chat_id, f"Добавлено {len(documents)} файла(ов). Всего в списке: {len(user_data['files_to_process'])}.")
    else:
        user_data['group_files_to_process'] = documents
        await context.bot.send_message(chat_id, f"Я получила {len(documents)} файла(ов). Что с ними сделать?", reply_markup=GROUP_ACTION_KEYBOARD)
    context.application.user_data[user_id] = user_data

async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.media_group_id:
        media_group_id = update.message.media_group_id; media_group_files[media_group_id].append(update.message.document)
        expected_action = context.user_data.get('awaiting_file_for')
        jobs = context.job_queue.get_jobs_by_name(str(media_group_id))
        for job in jobs: job.schedule_removal()
        context.job_queue.run_once(
            process_media_group, when=1.5,
            data={'media_group_id': media_group_id, 'chat_id': update.effective_chat.id, 'user_id': update.effective_user.id, 'action': expected_action},
            name=str(media_group_id))
        return context.conversation_state
    else:
        expected_action = context.user_data.pop('awaiting_file_for', None)
        if expected_action == 'split': return await split_file_handler(update, context)
        if expected_action == 'combine': return await receive_file_for_list(update, context, AWAIT_COMBINE_FILES)
        if expected_action == 'assembly_common': return await receive_assembly_common_file(update, context)
        if expected_action == 'assembly_unique': return await receive_file_for_list(update, context, AWAIT_ASSEMBLY_UNIQUE)
        if expected_action == 'pdf_to_img': return await ask_for_page_range(update, context)
        else:
            document = update.message.document
            if document.mime_type != 'application/pdf':
                await update.message.reply_text("Это не PDF-файл."); return CHOOSE_ACTION
            context.user_data['file_to_split'] = document; safe_filename = escape_markdown_v2(document.file_name)
            await update.message.reply_text(f"Я получила файл `{safe_filename}`\nКак именно вы хотите его разбить?", reply_markup=SPLIT_MODE_KEYBOARD, parse_mode='MarkdownV2')
            return CHOOSE_SPLIT_MODE

# --- НОВЫЙ БЛОК: ЛОГИКА "PDF В КАРТИНКИ" ---

def parse_page_ranges(range_str: str, max_pages: int) -> list[int]:
    if range_str.lower() == 'все': return list(range(max_pages))
    pages = set()
    try:
        parts = range_str.split(',');
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                for i in range(start, end + 1):
                    if 1 <= i <= max_pages: pages.add(i - 1)
            else:
                page_num = int(part)
                if 1 <= page_num <= max_pages: pages.add(page_num - 1)
    except ValueError: return []
    return sorted(list(pages))

async def ask_for_pdf_to_image_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Хорошо. Отправь мне PDF-файл, который нужно превратить в изображения.")
    context.user_data['awaiting_file_for'] = 'pdf_to_img'
    return AWAIT_PDF_TO_IMAGE_FILE

async def ask_for_page_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    try:
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        
        context.user_data['pdf_file_bytes'] = file_bytes
        context.user_data['pdf_page_count'] = pdf_doc.page_count
        base_name = os.path.splitext(document.file_name)[0]
        context.user_data['pdf_base_name'] = base_name

        await update.message.reply_text(
            f"Файл получен! В нем {pdf_doc.page_count} страниц.\n\nКакие страницы преобразовать? Отправь `все` или укажи номера/диапазоны (например: `1-3, 5`).",
            parse_mode='Markdown')
        pdf_doc.close()
        return AWAIT_PAGE_RANGE_FOR_IMAGE
    except Exception as e:
        logger.error(f"Ошибка при чтении PDF для конвертации в картинки: {e}")
        await update.message.reply_text("Не удалось прочитать этот PDF. Возможно, он поврежден.")
        return CHOOSE_ACTION

async def pdf_to_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    page_range_str = update.message.text
    max_pages = context.user_data.get('pdf_page_count', 0)
    page_indices = parse_page_ranges(page_range_str, max_pages)
    
    if not page_indices:
        await update.message.reply_text("Не поняла диапазон. Попробуй еще раз. Например: `1-3, 5` или `все`.", parse_mode='Markdown')
        return AWAIT_PAGE_RANGE_FOR_IMAGE
        
    await update.message.reply_text(f"Принято! Начинаю преобразование {len(page_indices)} страниц. Это может занять время...")
    
    try:
        file_bytes = context.user_data.get('pdf_file_bytes')
        base_name = context.user_data.get('pdf_base_name', 'document')
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        
        for page_index in page_indices:
            page = pdf_doc.load_page(page_index)
            pix = page.get_pixmap(dpi=200)
            
            # ИСПРАВЛЕНО: Генерируем байты и отправляем их напрямую, указывая filename
            img_bytes = pix.tobytes("png")
            
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=img_bytes, # Отправляем байты напрямую
                filename=f"{base_name}_page_{page_index + 1}.png" # Указываем имя файла
            )
        pdf_doc.close()
        final_message = "Готово! Все страницы отправлены в виде картинок."
    except Exception as e:
        logger.error(f"Ошибка при конвертации PDF в картинки: {e}")
        final_message = "Произошла ошибка во время преобразования. Попробуйте еще раз."
        
    return await return_to_main_menu(update, context, message=final_message)


# --- ОСТАЛЬНЫЕ СЦЕНАРИИ ---
async def ask_split_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text("Отлично! Как именно вы хотите разбить PDF файл?", reply_markup=SPLIT_MODE_KEYBOARD)
    return CHOOSE_SPLIT_MODE

async def handle_split_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['split_mode'] = query.data
    text_for_custom_order = "Хорошо\\. Отправьте порядок разбивки \\(например: `3,3,4`\\)"
    if 'file_to_split' in context.user_data:
        if query.data != 'split_custom':
            await query.edit_message_text("Поняла. Начинаю обработку...")
            return await split_file_handler(update, context, pre_saved=True)
        else:
            await query.edit_message_text(text_for_custom_order, parse_mode="MarkdownV2")
            return AWAIT_SPLIT_ORDER
    else:
        if query.data == 'split_custom':
            await query.edit_message_text(text_for_custom_order, parse_mode="MarkdownV2")
            return AWAIT_SPLIT_ORDER
        else:
            await query.edit_message_text("Поняла. Теперь просто отправьте мне PDF файл для разбивки.")
            context.user_data['awaiting_file_for'] = 'split'
            return AWAIT_SPLIT_FILE

async def receive_split_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order = update.message.text
    error_text = "Формат неверный\\. Используйте только цифры и запятые\\. Например: `3,3,4`"
    if not re.match(r'^\d+(,\s*\d+)*$', order):
        await update.message.reply_text(error_text, parse_mode="MarkdownV2")
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
    if pre_saved: document = context.user_data.get('file_to_split')
    else:
        document = update.message.document
        await update.message.reply_text("Файл принят. Начинаю обработку...")
    final_message = "Готово! Все части файла отправлены."
    try:
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        total_pages = pdf_doc.page_count; ranges, mode = [], context.user_data.get('split_mode')
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
            await context.bot.send_document(chat_id=update.effective_chat.id, document=new_doc.write(), filename=f"{base_name}_part_{i + 1}.pdf")
            new_doc.close()
        pdf_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при разбивке PDF: {e}"); final_message = "К сожалению, при обработке файла произошла ошибка."
    return await return_to_main_menu(update, context, message=final_message)

async def ask_for_combine_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); context.user_data.clear()
    context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Все файлы отправлены", callback_data="process_done")], [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]])
    await query.edit_message_text("Поняла. Отправляйте мне PDF файлы для объединения. Когда закончите, нажмите кнопку.", reply_markup=keyboard)
    context.user_data['awaiting_file_for'] = 'combine'
    return AWAIT_COMBINE_FILES

async def receive_file_for_list(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int):
    if 'files_to_process' not in context.user_data: context.user_data['files_to_process'] = []
    document = update.message.document; context.user_data['files_to_process'].append(document)
    context.user_data['awaiting_file_for'] = 'combine' if next_state == AWAIT_COMBINE_FILES else 'assembly_unique'
    await update.message.reply_text(f"Файл '{document.file_name}' добавлен ({len(context.user_data['files_to_process'])} всего).")
    return next_state

async def combine_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, from_group: bool = False) -> int:
    query = update.callback_query; await query.answer()
    documents = context.user_data.get('group_files_to_process') if from_group else context.user_data.get('files_to_process', [])
    if len(documents) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Нужно хотя бы два файла. Вы добавили {len(documents)}.")
        if not from_group:
            context.user_data['awaiting_file_for'] = 'combine'; return AWAIT_COMBINE_FILES
        else: return CHOOSE_ACTION
    await query.edit_message_text("Отлично! Начинаю объединение...")
    final_message = "Готово! Ваш объединенный файл."
    try:
        result_doc = fitz.open()
        for doc in documents:
            file = await context.bot.get_file(doc.file_id)
            file_bytes = await file.download_as_bytearray()
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf"); result_doc.insert_pdf(pdf_doc); pdf_doc.close()
        await context.bot.send_document(chat_id=update.effective_chat.id, document=result_doc.write(), filename="combined_document.pdf")
        result_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при объединении PDF: {e}"); final_message = "К сожалению, при обработке одного из файлов произошла ошибка."
    return await return_to_main_menu(update, context, message=final_message)

async def ask_for_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); context.user_data.clear()
    await query.edit_message_text("Хорошо. Сначала отправьте мне ОДИН общий PDF файл.")
    context.user_data['awaiting_file_for'] = 'assembly_common'
    return AWAIT_ASSEMBLY_COMMON

async def receive_assembly_common_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['common_file'] = update.message.document; context.user_data['files_to_process'] = []
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Собрать файлы", callback_data="process_done")], [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")]])
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
            unique_file = await context.bot.get_file(doc.file_id); unique_file_bytes = await unique_file.download_as_bytearray()
            result_doc = fitz.open(stream=unique_file_bytes, filetype="pdf"); common_pdf = fitz.open(stream=common_file_bytes, filetype="pdf")
            result_doc.insert_pdf(common_pdf); common_pdf.close()
            await context.bot.send_document(chat_id=update.effective_chat.id, document=result_doc.write(), filename=f"assembled_{doc.file_name}")
            result_doc.close()
    except Exception as e:
        logger.error(f"Ошибка при сборке PDF: {e}"); final_message = "К сожалению, при обработке одного из файлов произошла ошибка."
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
        ],
        states={
            CHOOSE_ACTION: [
                CallbackQueryHandler(ask_split_mode, pattern="^split$"),
                CallbackQueryHandler(ask_for_combine_files, pattern="^combine$"),
                CallbackQueryHandler(ask_for_assembly_common_file, pattern="^assembly$"),
                CallbackQueryHandler(ask_for_pdf_to_image_file, pattern="^pdf_to_img$"),
            ],
            CHOOSE_SPLIT_MODE: [
                CallbackQueryHandler(handle_split_choice, pattern="^split_(single|double|custom)$"),
            ],
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
            AWAIT_PDF_TO_IMAGE_FILE: [MessageHandler(filters.Document.PDF, document_router)],
            AWAIT_PAGE_RANGE_FOR_IMAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, pdf_to_image_handler)],
        },
        fallbacks=[CommandHandler("start", start), CallbackQueryHandler(main_menu, pattern="^main_menu$")],
        allow_reentry=True
    )
    
    application.add_handler(CallbackQueryHandler(lambda u, c: combine_files_handler(u, c, from_group=True), pattern="^group_combine$"))
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
