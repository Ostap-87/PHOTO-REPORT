"""Экспорт отчётов «ФотоОтчёта».

    export_docx(project, project_dir, out_path, options) -> str   # Word: титул, таблица исполнения, пункты с фото
    export_pdf(project, project_dir, out_path, options) -> str    # PDF A4 с тем же составом
    export_xlsx(project, project_dir, out_path) -> str            # Excel: листы «Исполнение» и «Фото»
    export_zip(project, project_dir, out_path, which="work") -> str   # фото и файлы по папкам пунктов + «Неразобранное/»
    export_registry_xlsx(project, out_path) -> str                # «Реестр подтверждений» + «Требуют уточнения»
    folder_name(item, index=None) -> str     # имя папки пункта в ZIP («01 1.2 Расстановка столов»)
    folder_names(project) -> {item_id: str}  # имена всех папок — ровно как внутри ZIP

options: {"photos_per_row": 2, "include_unsorted": False, "only_status": None,
          "max_px": 1600, "jpeg_quality": 85, "skip_duplicates": False}
Фото берутся из photos/work/<id>.jpg, если нет — из photos/original/, затем из photos/thumb/.

Необязательные поля (показываются, только если заполнены; без них отчёты прежние):
item.kind ("registry"|"tz"), item.section («Раздел»), item.required («Что приложить»),
item.note (комментарий анализа), item.links (ссылки), project["files"] — вложения
photos/files/<id><ext> ({"id","name","ext","size","item_id","added"}), project["clarify"].
"""

from .common import STATUS_COLORS, STATUS_LABELS, STATUS_ORDER
from .docx_report import export_docx
from .pdf_report import export_pdf, find_font
from .registry_xlsx import export_registry_xlsx
from .xlsx_report import export_xlsx
from .zip_export import UNSORTED_FOLDER, export_zip, folder_name, folder_names

__all__ = [
    "export_docx", "export_pdf", "export_xlsx", "export_zip", "export_registry_xlsx", "folder_name", "folder_names",
    "STATUS_LABELS", "STATUS_COLORS", "STATUS_ORDER", "UNSORTED_FOLDER", "find_font",
]
