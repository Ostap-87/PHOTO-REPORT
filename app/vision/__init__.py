"""Модуль фото «ФотоОтчёта»: чтение (io), оценка качества (quality), улучшение (enhance), лица (faces),
эмбеддинги CLIP (embed), раскладка по пунктам ТЗ (classify), почти-дубли (dedup).

Подмодули импортируются явно (``from app.vision import quality``); onnxruntime и модели
подгружаются только при первом обращении к embed.Embedder.
"""
