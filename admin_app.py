import re
import shutil
from datetime import datetime
from pathlib import Path

import base64
import json
import io
import os
import streamlit as st
from PIL import Image
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
try:
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    pytesseract = None
    OCR_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = BASE_DIR / "photos"
DATA_JS = BASE_DIR / "data.js"

st.set_page_config(page_title="トッピング図鑑（登録画面）", layout="centered")
st.markdown(
    """
    <style>
      .main .block-container {
        max-width: 760px;
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"] {
        background: #f1f3f6;
        border: 1px solid #d5dbe3;
        border-radius: 10px 10px 0 0;
        color: #2a3340;
        font-weight: 700;
        padding: 10px 16px;
        margin-right: 6px;
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        background: #1f3d31;
        color: #ffffff;
        border-color: #1f3d31;
      }
      div[data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 2px;
      }
      div[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
        background: transparent;
      }
      div[data-testid="stFileUploaderDropzone"] {
        min-height: 1600px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- Utility ----------
def zenkaku_len(s: str) -> int:
    # ざっくり「全角換算」: 文字数=コードポイント数で扱う
    return len(s)

def sanitize_folder_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\\/:"*?<>|]+', "", name)
    return name

def ensure_paths():
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_JS.exists():
        write_records([])

def parse_records_from_js(text: str) -> list[dict]:
    match = re.search(r"window\.PHOTO_DATA\s*=\s*(\[[\s\S]*\])\s*;", text)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass

    # 壊れた data.js から最低限復元するフォールバック
    restored = []
    for obj_match in re.finditer(r"\{[\s\S]*?\}", text):
        chunk = obj_match.group(0)
        try:
            item = json.loads(chunk)
            if isinstance(item, dict):
                restored.append(item)
        except json.JSONDecodeError:
            continue
    return restored

def load_records() -> list[dict]:
    ensure_paths()
    text = DATA_JS.read_text(encoding="utf-8")
    records = parse_records_from_js(text)
    for row in records:
        if "ocr_text" not in row:
            row["ocr_text"] = ""
    return sorted(records, key=lambda x: x.get("id", ""), reverse=True)

def write_records(records: list[dict]) -> None:
    content = "window.PHOTO_DATA = " + json.dumps(records, ensure_ascii=False, indent=2) + ";\n"
    DATA_JS.write_text(content, encoding="utf-8")

def collect_categories(records: list[dict]) -> list[str]:
    categories = {sanitize_folder_name(r.get("category", "")) for r in records}
    for p in PHOTOS_DIR.iterdir():
        if p.is_dir():
            categories.add(p.name)
    categories.discard("")
    return sorted(categories)

def unique_file_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

def to_abs_path(rel_path: str) -> Path:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    return BASE_DIR / normalized

def save_image(file, category: str) -> str:
    category_dir = PHOTOS_DIR / category
    category_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.name).suffix.lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".jpg"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{sanitize_folder_name(Path(file.name).stem)[:20]}{ext}"
    out_path = unique_file_path(category_dir, filename)

    raw_bytes = bytes(file.getbuffer())
    if ext == ".png":
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                has_alpha = False
                if img.mode in ("RGBA", "LA"):
                    alpha = img.getchannel("A")
                    has_alpha = alpha.getextrema()[0] < 255
                elif img.mode == "P" and "transparency" in img.info:
                    has_alpha = True

                if has_alpha:
                    rgba = img.convert("RGBA")
                    white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    white_bg.alpha_composite(rgba)
                    white_bg.convert("RGB").save(out_path, format="PNG")
                else:
                    out_path.write_bytes(raw_bytes)
        except Exception:
            out_path.write_bytes(raw_bytes)
    else:
        out_path.write_bytes(raw_bytes)

    return "./" + out_path.relative_to(BASE_DIR).as_posix()

def move_record_image(old_rel_path: str, new_category: str) -> str:
    old_abs = to_abs_path(old_rel_path)
    if not old_abs.exists():
        return old_rel_path

    new_dir = PHOTOS_DIR / new_category
    new_dir.mkdir(parents=True, exist_ok=True)
    new_abs = unique_file_path(new_dir, old_abs.name)
    shutil.move(str(old_abs), str(new_abs))
    return "./" + new_abs.relative_to(BASE_DIR).as_posix()

def delete_record_image(rel_path: str) -> None:
    target = to_abs_path(rel_path)
    if target.exists() and target.is_file():
        target.unlink()

def normalize_ocr_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:1000]

def extract_ocr_text(image_path: Path) -> str:
    if not OCR_AVAILABLE:
        return ""
    if not image_path.exists():
        return ""

    try:
        with Image.open(image_path) as img:
            base = img.convert("RGB")
            gray = base.convert("L")

        for lang in ("jpn+eng", "eng"):
            try:
                result = pytesseract.image_to_string(gray, lang=lang, config="--oem 3 --psm 6")
                cleaned = normalize_ocr_text(result)
                if cleaned:
                    return cleaned
            except Exception:
                continue
    except Exception:
        return ""

    return ""

def normalize_hash_tag(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.lstrip("#").strip()
    if not cleaned:
        return ""
    return f"#{cleaned}"

def split_description_to_tag_inputs(description: str, count: int = 5) -> list[str]:
    source = (description or "").strip()
    tags = [t.strip() for t in re.findall(r"#([^\s#]+)", source) if t.strip()]
    if not tags and source:
        tags = [source.lstrip("#").strip()]
    tags = tags[:count]
    return tags + [""] * (count - len(tags))

def get_secret_value(*keys: str) -> str:
    try:
        current = st.secrets
        for key in keys:
            current = current[key]
        return str(current).strip()
    except Exception:
        return ""

def get_github_sync_config() -> dict:
    token = (
        os.getenv("GITHUB_TOKEN", "").strip()
        or get_secret_value("GITHUB_TOKEN")
        or get_secret_value("github", "token")
    )
    repo = (
        os.getenv("GITHUB_REPO", "").strip()
        or get_secret_value("GITHUB_REPO")
        or get_secret_value("github", "repo")
    )
    branch = (
        os.getenv("GITHUB_BRANCH", "").strip()
        or get_secret_value("GITHUB_BRANCH")
        or get_secret_value("github", "branch")
        or "main"
    )
    enabled_raw = (
        os.getenv("GITHUB_SYNC_ENABLED", "").strip()
        or get_secret_value("GITHUB_SYNC_ENABLED")
        or get_secret_value("github", "enabled")
    ).lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"} if enabled_raw else bool(token and repo)
    if not enabled or not token or not repo:
        return {}
    return {
        "token": token,
        "repo": repo,
        "branch": branch,
    }

def normalize_repo_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    normalized = normalized.lstrip("./")
    return normalized

def github_api_request(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "topping-zukan-sync",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=25) as res:
            text = res.read().decode("utf-8")
            data = json.loads(text) if text else {}
            return res.getcode(), data
    except urlerror.HTTPError as e:
        text = e.read().decode("utf-8", errors="ignore")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"message": text}
        return e.code, data
    except Exception as e:
        return 0, {"message": str(e)}

def github_get_file_sha(repo_path: str, config: dict) -> str:
    encoded_path = urlparse.quote(repo_path, safe="/")
    encoded_branch = urlparse.quote(config["branch"], safe="")
    code, data = github_api_request(
        "GET",
        f"/repos/{config['repo']}/contents/{encoded_path}?ref={encoded_branch}",
        config["token"],
    )
    if code == 200 and isinstance(data, dict):
        return str(data.get("sha", ""))
    if code == 404:
        return ""
    raise RuntimeError(data.get("message", f"GitHub API error ({code})"))

def github_upsert_file(repo_path: str, content_bytes: bytes, commit_message: str, config: dict) -> None:
    encoded_path = urlparse.quote(repo_path, safe="/")
    sha = github_get_file_sha(repo_path, config)
    payload = {
        "message": commit_message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": config["branch"],
    }
    if sha:
        payload["sha"] = sha

    code, data = github_api_request(
        "PUT",
        f"/repos/{config['repo']}/contents/{encoded_path}",
        config["token"],
        payload,
    )
    if code not in (200, 201):
        raise RuntimeError(data.get("message", f"GitHub API error ({code})"))

def github_delete_file(repo_path: str, commit_message: str, config: dict) -> None:
    sha = github_get_file_sha(repo_path, config)
    if not sha:
        return
    encoded_path = urlparse.quote(repo_path, safe="/")
    payload = {
        "message": commit_message,
        "sha": sha,
        "branch": config["branch"],
    }
    code, data = github_api_request(
        "DELETE",
        f"/repos/{config['repo']}/contents/{encoded_path}",
        config["token"],
        payload,
    )
    if code not in (200, 202):
        raise RuntimeError(data.get("message", f"GitHub API error ({code})"))

def sync_changes_to_github(changed_paths: list[str], deleted_paths: list[str], action_label: str) -> tuple[bool, str]:
    config = get_github_sync_config()
    if not config:
        return False, "GitHub同期は未設定です（ローカル保存のみ）。"

    changed_repo_paths = []
    deleted_repo_paths = []
    seen_changed = set()
    seen_deleted = set()

    for path in changed_paths:
        rp = normalize_repo_path(path)
        if not rp or rp in seen_changed:
            continue
        seen_changed.add(rp)
        changed_repo_paths.append(rp)

    for path in deleted_paths:
        rp = normalize_repo_path(path)
        if not rp or rp == "data.js" or rp in seen_deleted:
            continue
        seen_deleted.add(rp)
        deleted_repo_paths.append(rp)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        for repo_path in changed_repo_paths:
            abs_path = BASE_DIR / repo_path
            if not abs_path.exists() or not abs_path.is_file():
                continue
            github_upsert_file(
                repo_path,
                abs_path.read_bytes(),
                f"{action_label} {timestamp} - update {repo_path}",
                config,
            )

        for repo_path in deleted_repo_paths:
            github_delete_file(
                repo_path,
                f"{action_label} {timestamp} - delete {repo_path}",
                config,
            )
    except Exception as e:
        return False, f"GitHub同期エラー: {e}"

    return True, f"GitHub同期完了: {config['repo']} ({config['branch']})"

# ---------- UI ----------
st.markdown(
    '<h1 style="margin:0;">トッピング図鑑 <span style="font-size:70%;">（登録画面）</span></h1>',
    unsafe_allow_html=True,
)

st.markdown("写真登録・検索・修正・削除をこの画面で行います。")
ensure_paths()
sync_config = get_github_sync_config()
if sync_config:
    st.caption(f"GitHub同期: ON（{sync_config['repo']} / {sync_config['branch']}）")
else:
    st.caption("GitHub同期: OFF（Secretsまたは環境変数が未設定）")

tab_register, tab_manage = st.tabs(["写真を登録", "検索・修正・削除"])

with tab_register:
    records = load_records()
    cats = collect_categories(records)

    st.subheader("1) 写真を追加")
    uploaded_files = st.file_uploader(
        "写真をドラッグ&ドロップ（jpg/png/webp）",
        type=["jpg", "jpeg", "png", "webp"],
        key="register_upload",
        accept_multiple_files=True,
    )

    st.subheader("2) 情報を入力")
    category_placeholder = "(登録済みアイテムから選択)"
    category_choice = st.selectbox("アイテムの種類", [category_placeholder] + cats, key="register_category_choice")
    new_category = st.text_input("新規の種類名（最大全角20文字）", max_chars=20, key="register_new_category")
    category = new_category.strip() if category_choice == category_placeholder else category_choice

    title = st.text_input("写真タイトル（最大全角40文字）", max_chars=40, key="register_title")
    st.markdown("ハッシュタグ（各最大全角30文字）")
    tag_cols = st.columns(5)
    register_tags = []
    for i, col in enumerate(tag_cols, start=1):
        with col:
            register_tags.append(
                st.text_input(
                    f"タグ{i}",
                    max_chars=30,
                    key=f"register_tag_{i}",
                    label_visibility="collapsed",
                    placeholder=f"タグ{i}",
                )
            )
    if OCR_AVAILABLE:
        st.caption("画像内テキスト（OCR）も自動で検索対象になります。")
    else:
        st.caption("OCR未設定のため、現在は画像内テキスト検索は無効です。")

    st.subheader("3) 登録")
    disabled = (
        not uploaded_files
        or category.strip() == ""
        or title.strip() == ""
    )

    if st.button("登録する", type="primary", disabled=disabled):
        cat = sanitize_folder_name(category)
        if zenkaku_len(cat) > 20:
            st.error("種類が長すぎます（20文字以内）")
            st.stop()
        if zenkaku_len(title.strip()) > 40:
            st.error("タイトルが長すぎます（40文字以内）")
            st.stop()
        for raw_tag in register_tags:
            if zenkaku_len((raw_tag or "").strip()) > 30:
                st.error("各ハッシュタグは30文字以内で入力してください。")
                st.stop()

        saved_paths = []
        base_title = title.strip()
        normalized_tags = [normalize_hash_tag(t) for t in register_tags]
        normalized_tags = [t for t in normalized_tags if t]
        base_desc = " ".join(normalized_tags)
        for i, up in enumerate(uploaded_files):
            rec_id = datetime.now().strftime("%Y%m%d%H%M%S%f") + str(i).zfill(2)
            img_path = save_image(up, cat)
            ocr_text = extract_ocr_text(to_abs_path(img_path))
            records.append(
                {
                    "id": rec_id,
                    "category": cat,
                    "title": base_title,
                    "description": base_desc,
                    "path": img_path,
                    "ocr_text": ocr_text,
                }
            )
            saved_paths.append(img_path)
        write_records(records)
        sync_ok, sync_message = sync_changes_to_github(
            ["./data.js"] + saved_paths,
            [],
            "register",
        )

        st.success(f"{len(saved_paths)}件を登録しました。")
        if len(saved_paths) == 1:
            st.write("保存先:", saved_paths[0])
        else:
            st.write("保存先（先頭3件）:", ", ".join(saved_paths[:3]))
        if sync_ok:
            st.success(sync_message)
        else:
            st.warning(sync_message)
        st.rerun()


with tab_manage:
    records = load_records()
    cats = collect_categories(records)
    st.subheader("検索")
    col1, col2 = st.columns([2, 1])
    with col1:
        keyword = st.text_input("キーワード（種類・タイトル・説明文・画像内文字）", key="filter_keyword")
    with col2:
        filter_cat = st.selectbox("種類", ["すべて"] + cats, key="filter_category")

    if OCR_AVAILABLE:
        if st.button("既存データのOCRを再抽出（全件）", key="reindex_ocr"):
            with st.spinner("OCR抽出中..."):
                updated = 0
                for row in records:
                    rel = row.get("path", "")
                    if not rel:
                        continue
                    row["ocr_text"] = extract_ocr_text(to_abs_path(rel))
                    updated += 1
                write_records(records)
                sync_ok, sync_message = sync_changes_to_github(
                    ["./data.js"],
                    [],
                    "reindex-ocr",
                )
            st.success(f"OCRを{updated}件更新しました。")
            if sync_ok:
                st.success(sync_message)
            else:
                st.warning(sync_message)
            st.rerun()
    else:
        st.info("画像内テキスト検索を有効にするには、`pytesseract` と `tesseract` の導入が必要です。")

    filtered = []
    for r in records:
        haystack = f"{r.get('category','')} {r.get('title','')} {r.get('description','')} {r.get('ocr_text','')}".lower()
        if keyword.strip() and keyword.strip().lower() not in haystack:
            continue
        if filter_cat != "すべて" and r.get("category") != filter_cat:
            continue
        filtered.append(r)

    st.caption(f"{len(filtered)}件ヒット")
    if not filtered:
        st.info("該当データがありません。")
    else:
        for r in filtered:
            rec_id = r.get("id", "")
            with st.expander(f"【{r.get('category','')}】{r.get('title','')}"):
                st.image(r.get("path", ""), width=240)
                st.caption(f"ID: {rec_id}")

                current_cat = r.get("category", "")
                other_cats = [c for c in cats if c != current_cat]
                cat_options = [current_cat] + other_cats + ["(新しく入力)"]
                cat_default_index = 0
                selected_cat = st.selectbox(
                    "種類（編集）",
                    cat_options,
                    index=cat_default_index,
                    key=f"edit_cat_select_{rec_id}",
                )
                custom_cat = st.text_input(
                    "新規の種類名（編集時）",
                    max_chars=20,
                    key=f"edit_cat_new_{rec_id}",
                )
                edited_cat = custom_cat.strip() if selected_cat == "(新しく入力)" else selected_cat

                edited_title = st.text_input(
                    "タイトル",
                    value=r.get("title", ""),
                    max_chars=40,
                    key=f"edit_title_{rec_id}",
                )
                st.markdown("ハッシュタグ（編集・各最大全角30文字）")
                edit_tags_default = split_description_to_tag_inputs(r.get("description", ""), 5)
                edit_tag_cols = st.columns(5)
                edited_tags = []
                for i, col in enumerate(edit_tag_cols, start=1):
                    with col:
                        edited_tags.append(
                            st.text_input(
                                f"編集タグ{i}",
                                value=edit_tags_default[i - 1],
                                max_chars=30,
                                key=f"edit_tag_{rec_id}_{i}",
                                label_visibility="collapsed",
                                placeholder=f"タグ{i}",
                            )
                        )
                replacement = st.file_uploader(
                    "写真を差し替える（任意）",
                    type=["jpg", "jpeg", "png", "webp"],
                    key=f"edit_file_{rec_id}",
                )

                col_save, col_delete = st.columns(2)
                with col_save:
                    if st.button("更新する", key=f"update_{rec_id}"):
                        cat = sanitize_folder_name(edited_cat)
                        if cat == "":
                            st.error("種類を入力してください。")
                            st.stop()
                        if zenkaku_len(cat) > 20:
                            st.error("種類が長すぎます（20文字以内）")
                            st.stop()
                        if zenkaku_len(edited_title.strip()) > 40:
                            st.error("タイトルが長すぎます（40文字以内）")
                            st.stop()
                        for raw_tag in edited_tags:
                            if zenkaku_len((raw_tag or "").strip()) > 30:
                                st.error("各ハッシュタグは30文字以内で入力してください。")
                                st.stop()

                        updated_path = r.get("path", "")
                        original_path = updated_path
                        updated_ocr = r.get("ocr_text", "")
                        updated_desc = " ".join(
                            [tag for tag in (normalize_hash_tag(t) for t in edited_tags) if tag]
                        )
                        changed_for_sync = ["./data.js"]
                        deleted_for_sync = []
                        if replacement is not None:
                            new_path = save_image(replacement, cat)
                            delete_record_image(updated_path)
                            deleted_for_sync.append(updated_path)
                            updated_path = new_path
                            changed_for_sync.append(updated_path)
                            updated_ocr = extract_ocr_text(to_abs_path(updated_path))
                        elif cat != r.get("category", ""):
                            updated_path = move_record_image(updated_path, cat)
                            if updated_path != original_path:
                                deleted_for_sync.append(original_path)
                                changed_for_sync.append(updated_path)

                        for row in records:
                            if row.get("id") == rec_id:
                                row["category"] = cat
                                row["title"] = edited_title.strip()
                                row["description"] = updated_desc
                                row["path"] = updated_path
                                row["ocr_text"] = updated_ocr
                                break

                        write_records(records)
                        sync_ok, sync_message = sync_changes_to_github(
                            changed_for_sync,
                            deleted_for_sync,
                            "update",
                        )
                        st.success("更新しました。")
                        if sync_ok:
                            st.success(sync_message)
                        else:
                            st.warning(sync_message)
                        st.rerun()

                with col_delete:
                    confirm = st.checkbox("削除を確定する", key=f"confirm_{rec_id}")
                    if st.button("削除する", key=f"delete_{rec_id}", disabled=not confirm):
                        deleted_image_path = r.get("path", "")
                        delete_record_image(deleted_image_path)
                        records = [row for row in records if row.get("id") != rec_id]
                        write_records(records)
                        sync_ok, sync_message = sync_changes_to_github(
                            ["./data.js"],
                            [deleted_image_path],
                            "delete",
                        )
                        st.warning("削除しました。")
                        if sync_ok:
                            st.success(sync_message)
                        else:
                            st.warning(sync_message)
                        st.rerun()
