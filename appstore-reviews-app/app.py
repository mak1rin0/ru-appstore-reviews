from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Iterable, Optional

import pandas as pd
import requests
import streamlit as st

DEFAULT_APP_ID = "570060128"
RSS_URL_TEMPLATE = (
    "https://itunes.apple.com/ru/rss/customerreviews/"
    "page={page}/id={app_id}/sortBy=mostRecent/json"
)


# =====================================================================
# Логика сбора, фильтрации и экспорта данных
# =====================================================================

def extract_app_id(app_id_or_url: str) -> str:
    """Извлекает числовой ID приложения из строки или ссылки App Store."""
    if not isinstance(app_id_or_url, str):
        raise ValueError("ID приложения должен быть строкой.")

    cleaned = app_id_or_url.strip()
    if not cleaned:
        raise ValueError("Введите числовой ID приложения или ссылку на App Store.")

    if cleaned.isdigit():
        return cleaned

    import re

    match = re.search(r"(?:id|/)(\d{5,15})(?:[/?#&]|$)", cleaned)
    if not match:
        raise ValueError(
            f"Не удалось распознать App Store ID в строке: '{app_id_or_url}'."
        )
    return match.group(1)


def _parse_iso_datetime(raw_date: Optional[str]) -> Optional[datetime]:
    """Парсит строку даты из RSS в наивный UTC datetime."""
    if not raw_date:
        return None
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _safe_label(entry: dict, key: str) -> str:
    """Извлекает текстовое поле .label из словаря RSS."""
    value = entry.get(key)
    if isinstance(value, dict):
        label = value.get("label")
        return label.strip() if isinstance(label, str) else ""
    if isinstance(value, str):
        return value.strip()
    return ""


def fetch_ru_reviews(
    app_id_or_url: str = DEFAULT_APP_ID,
    max_pages: int = 10,
    timeout: float = 10.0,
) -> pd.DataFrame:
    """Загружает отзывы из российского App Store (ru) через JSON RSS."""
    app_id = extract_app_id(app_id_or_url)
    columns = [
        "review_id",
        "date",
        "rating",
        "title",
        "review_text",
        "author",
        "app_version",
    ]

    rows: list[dict] = []
    seen_ids: set[str] = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RUAppStoreAnalyzer/1.0)",
        "Accept": "application/json",
    }

    with requests.Session() as session:
        for page in range(1, max_pages + 1):
            url = RSS_URL_TEMPLATE.format(page=page, app_id=app_id)
            try:
                resp = session.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.Timeout, requests.RequestException, ValueError):
                break

            feed = data.get("feed") if isinstance(data, dict) else None
            entries = feed.get("entry") if isinstance(feed, dict) else None

            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list) or not entries:
                break

            added_on_page = 0
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rating_str = _safe_label(entry, "im:rating")
                if not rating_str.isdigit():
                    continue
                rating = int(rating_str)
                if not (1 <= rating <= 5):
                    continue

                review_id = _safe_label(entry, "id")
                if review_id and review_id in seen_ids:
                    continue
                if review_id:
                    seen_ids.add(review_id)

                author_block = entry.get("author")
                author = _safe_label(author_block, "name") if isinstance(author_block, dict) else ""

                rows.append(
                    {
                        "review_id": review_id,
                        "date": _parse_iso_datetime(_safe_label(entry, "updated")),
                        "rating": rating,
                        "title": _safe_label(entry, "title"),
                        "review_text": _safe_label(entry, "content"),
                        "author": author,
                        "app_version": _safe_label(entry, "im:version"),
                    }
                )
                added_on_page += 1

            if added_on_page == 0:
                break

    if not rows:
        empty_df = pd.DataFrame(columns=columns)
        empty_df["date"] = pd.to_datetime(empty_df["date"])
        empty_df["rating"] = empty_df["rating"].astype(int)
        return empty_df

    df = pd.DataFrame(rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["rating"]).copy()
    df["rating"] = df["rating"].astype(int)
    return df.sort_values("date", ascending=False, na_position="last").reset_index(drop=True)


def filter_reviews(
    df: pd.DataFrame,
    ratings: Optional[Iterable[int]] = None,
    date_from: Optional[date | datetime] = None,
    date_to: Optional[date | datetime] = None,
) -> pd.DataFrame:
    """Фильтрует датафрейм отзывов по оценкам и диапазону дат."""
    if df.empty:
        return df.copy()

    filtered = df.copy()
    filtered["date"] = pd.to_datetime(filtered["date"], errors="coerce")

    if ratings is not None:
        selected = {int(r) for r in ratings}
        filtered = filtered[filtered["rating"].isin(selected)]

    if date_from is not None:
        start_dt = (
            date_from
            if isinstance(date_from, datetime)
            else datetime.combine(date_from, time.min)
        )
        filtered = filtered[filtered["date"].notna() & (filtered["date"] >= start_dt)]

    if date_to is not None:
        end_dt = (
            date_to
            if isinstance(date_to, datetime)
            else datetime.combine(date_to, time.max)
        )
        filtered = filtered[filtered["date"].notna() & (filtered["date"] <= end_dt)]

    return filtered.reset_index(drop=True)


def build_csv_bytes(df: pd.DataFrame) -> bytes:
    """Формирует CSV в UTF-8 с BOM (utf-8-sig) для корректного открытия в Excel."""
    export_df = df.copy()
    if "date" in export_df.columns:
        dt_series = pd.to_datetime(export_df["date"], errors="coerce")
        export_df["date"] = dt_series.dt.strftime("%Y-%m-%d %H:%M").fillna("")
    return export_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


@st.cache_data(ttl=1800, show_spinner=False)
def cached_fetch_ru_reviews(app_id_or_url: str, max_pages: int = 10) -> pd.DataFrame:
    """Кэширует выгрузку отзывов на 30 минут."""
    return fetch_ru_reviews(app_id_or_url=app_id_or_url, max_pages=max_pages)


# =====================================================================
# Пользовательский интерфейс Streamlit
# =====================================================================

def render_sidebar_filters(df: pd.DataFrame) -> tuple[list[int], Optional[date], Optional[date]]:
    """Рендерит фильтры по оценкам и датам в сайдбаре."""
    st.sidebar.header("Фильтры отзывов")

    selected_ratings = st.sidebar.multiselect(
        "Оценки (звёзды)",
        options=[1, 2, 3, 4, 5],
        default=[1, 2, 3, 4, 5],
        help="Выберите одну или несколько оценок для анализа.",
    )

    valid_dates = df["date"].dropna() if not df.empty else pd.Series(dtype="datetime64[ns]")
    if valid_dates.empty:
        st.sidebar.caption("Диапазон дат станет доступен после загрузки отзывов.")
        return selected_ratings, None, None

    min_d = valid_dates.min().date()
    max_d = valid_dates.max().date()

    date_range = st.sidebar.date_input(
        "Диапазон дат публикации",
        value=(min_d, max_d),
        min_value=min_d,
        max_value=max_d,
        help="Ограничьте выборку по дате отзыва (UTC).",
    )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        d_from, d_to = date_range
    elif isinstance(date_range, tuple) and len(date_range) == 1:
        d_from, d_to = date_range[0], max_d
    else:
        d_from, d_to = min_d, max_d

    return selected_ratings, d_from, d_to


def main() -> None:
    st.set_page_config(
        page_title="RU App Store Reviews Explorer",
        page_icon="📱",
        layout="wide",
    )

    st.title("Анализ отзывов из российского App Store (`ru`)")
    st.caption(
        "Инструмент для продуктовой аналитики конкурентов в сегменте EdTech "
        "(по умолчанию настроен на Duolingo)."
    )

    with st.expander("Где взять App Store ID?", expanded=False):
        st.markdown(
            "1. Откройте страницу приложения в браузере на `apps.apple.com`.\n"
            "2. В ссылке вида `https://apps.apple.com/ru/app/duolingo-языки/id570060128` "
            "число после `id` (например, `570060128`) — это App Store ID.\n"
            "3. Можно вставить как сам ID, так и полную ссылку."
        )

    col_input, col_btn = st.columns([4, 1], vertical_alignment="bottom")
    with col_input:
        app_input = st.text_input(
            "App Store ID или ссылка на приложение",
            value=DEFAULT_APP_ID,
            placeholder="Например: 570060128 или https://apps.apple.com/ru/app/.../id570060128",
        )
    with col_btn:
        fetch_clicked = st.button("Загрузить отзывы", type="primary", use_container_width=True)

    # Инициализация состояния при первом открытии
    if "reviews_df" not in st.session_state:
        st.session_state["reviews_df"] = pd.DataFrame()
        st.session_state["resolved_app_id"] = DEFAULT_APP_ID

    if fetch_clicked:
        try:
            resolved_id = extract_app_id(app_input)
            with st.spinner(f"Загружаем отзывы из RU App Store (ID: {resolved_id})..."):
                df_loaded = cached_fetch_ru_reviews(app_input, max_pages=10)
            st.session_state["reviews_df"] = df_loaded
            st.session_state["resolved_app_id"] = resolved_id
        except ValueError as exc:
            st.error(str(exc))
            return

    df_raw: pd.DataFrame = st.session_state["reviews_df"]
    resolved_id: str = st.session_state["resolved_app_id"]

    if df_raw.empty:
        st.info(
            "Нажмите **«Загрузить отзывы»**, чтобы получить данные из российского App Store "
            "(RSS отдаёт до 10 страниц самых свежих отзывов)."
        )
        return

    selected_ratings, d_from, d_to = render_sidebar_filters(df_raw)
    df_filtered = filter_reviews(
        df_raw,
        ratings=selected_ratings,
        date_from=d_from,
        date_to=d_to,
    )

    st.divider()

    # Метрики по отфильтрованной выборке
    m1, m2, m3 = st.columns(3)
    total_count = len(df_filtered)
    avg_rating = f"{df_filtered['rating'].mean():.2f} ★" if total_count > 0 else "—"
    share_1_3 = (
        f"{(df_filtered['rating'] <= 3).mean() * 100:.1f}%" if total_count > 0 else "—"
    )

    m1.metric("Отзывов в выборке", f"{total_count} из {len(df_raw)}")
    m2.metric("Средняя оценка", avg_rating)
    m3.metric("Доля оценок 1–3★ (риск оттока)", share_1_3)

    # Распределение по звёздам
    st.subheader("Распределение по оценкам")
    rating_counts = (
        df_filtered["rating"]
        .value_counts()
        .reindex([1, 2, 3, 4, 5], fill_value=0)
        .rename_axis("Оценка")
        .to_frame(name="Количество отзывов")
    )
    rating_counts.index = [f"{i}★" for i in rating_counts.index]
    st.bar_chart(rating_counts)

    # Таблица отзывов и экспорт в CSV
    st.subheader("Отзывы")
    display_df = df_filtered.copy()
    if "date" in display_df.columns:
        display_df["date"] = pd.to_datetime(display_df["date"], errors="coerce").dt.strftime(
            "%Y-%m-%d %H:%M"
        )

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "review_id": "ID отзыва",
            "date": "Дата (UTC)",
            "rating": st.column_config.NumberColumn("Оценка", format="%d ★"),
            "title": "Заголовок",
            "review_text": "Текст отзыва",
            "author": "Автор",
            "app_version": "Версия",
        },
    )

    csv_bytes = build_csv_bytes(df_filtered)
    file_name = f"appstore_ru_{resolved_id}_reviews.csv"
    st.download_button(
        label="Скачать отфильтрованные отзывы в CSV",
        data=csv_bytes,
        file_name=file_name,
        mime="text/csv",
    )


if __name__ == "__main__":
    main()