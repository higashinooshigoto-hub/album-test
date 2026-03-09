<?php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');

$BASE_DIR = __DIR__;
$PHOTOS_DIR = $BASE_DIR . '/photos';
$DATA_JS = $BASE_DIR . '/data.js';

if (!is_dir($PHOTOS_DIR)) {
    @mkdir($PHOTOS_DIR, 0775, true);
}
if (!file_exists($DATA_JS)) {
    @file_put_contents($DATA_JS, "window.PHOTO_DATA = [];\n");
}

function respond(array $payload, int $status = 200): void {
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function zenkaku_len(string $s): int {
    return mb_strlen($s, 'UTF-8');
}

function sanitize_folder_name(string $name): string {
    $name = trim($name);
    $name = preg_replace('/[\\\\\\/:\"*?<>|]+/u', '', $name);
    return trim((string)$name);
}

function sanitize_filename_stem(string $stem): string {
    $stem = trim($stem);
    $stem = preg_replace('/[\\\\\\/:\"*?<>|]+/u', '', $stem);
    $stem = preg_replace('/\s+/u', '_', (string)$stem);
    $stem = preg_replace('/[^\p{L}\p{N}_\-.]+/u', '', (string)$stem);
    $stem = trim((string)$stem, '._');
    if ($stem === '') {
        $stem = 'image';
    }
    if (mb_strlen($stem, 'UTF-8') > 20) {
        $stem = mb_substr($stem, 0, 20, 'UTF-8');
    }
    return $stem;
}

function normalize_tag(string $raw): string {
    $tag = trim($raw);
    $tag = ltrim($tag, '#');
    $tag = trim($tag);
    if ($tag === '') {
        return '';
    }
    return '#' . $tag;
}

function split_description_to_tags(string $description, int $count = 5): array {
    $source = trim($description);
    $tags = [];
    if ($source !== '') {
        if (preg_match_all('/#([^\s#]+)/u', $source, $m)) {
            foreach ($m[1] as $v) {
                $v = trim((string)$v);
                if ($v !== '') {
                    $tags[] = $v;
                }
            }
        }
        if (empty($tags)) {
            $plain = ltrim($source, '#');
            $plain = trim($plain);
            if ($plain !== '') {
                $tags[] = $plain;
            }
        }
    }
    $tags = array_slice($tags, 0, $count);
    while (count($tags) < $count) {
        $tags[] = '';
    }
    return $tags;
}

function build_description_from_tags(array $tags): string {
    $normalized = [];
    foreach ($tags as $t) {
        $tag = normalize_tag((string)$t);
        if ($tag !== '') {
            $normalized[] = $tag;
        }
    }
    return implode(' ', $normalized);
}

function parse_records_from_js(string $text): array {
    if (preg_match('/window\.PHOTO_DATA\s*=\s*(\[[\s\S]*\])\s*;/u', $text, $m)) {
        $decoded = json_decode($m[1], true);
        if (is_array($decoded)) {
            $rows = [];
            foreach ($decoded as $row) {
                if (is_array($row)) {
                    if (!isset($row['ocr_text'])) {
                        $row['ocr_text'] = '';
                    }
                    $rows[] = $row;
                }
            }
            return $rows;
        }
    }
    return [];
}

function load_records(string $data_js): array {
    if (!file_exists($data_js)) {
        return [];
    }
    $text = (string)file_get_contents($data_js);
    $rows = parse_records_from_js($text);
    usort($rows, static function ($a, $b) {
        return strcmp((string)($b['id'] ?? ''), (string)($a['id'] ?? ''));
    });
    return $rows;
}

function write_records(string $data_js, array $records): void {
    $content = 'window.PHOTO_DATA = ' . json_encode(
        array_values($records),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT
    ) . ";\n";
    if (@file_put_contents($data_js, $content, LOCK_EX) === false) {
        respond(['ok' => false, 'error' => 'data.jsの保存に失敗しました。'], 500);
    }
}

function collect_categories(array $records, string $photos_dir): array {
    $map = [];
    foreach ($records as $r) {
        $cat = sanitize_folder_name((string)($r['category'] ?? ''));
        if ($cat !== '') {
            $map[$cat] = true;
        }
    }
    if (is_dir($photos_dir)) {
        $entries = scandir($photos_dir);
        if (is_array($entries)) {
            foreach ($entries as $name) {
                if ($name === '.' || $name === '..') {
                    continue;
                }
                if (is_dir($photos_dir . '/' . $name)) {
                    $map[$name] = true;
                }
            }
        }
    }
    $cats = array_keys($map);
    sort($cats, SORT_NATURAL | SORT_FLAG_CASE);
    return $cats;
}

function unique_file_path(string $directory, string $filename): string {
    $candidate = $directory . '/' . $filename;
    if (!file_exists($candidate)) {
        return $candidate;
    }
    $dot = strrpos($filename, '.');
    $stem = $dot === false ? $filename : substr($filename, 0, $dot);
    $ext = $dot === false ? '' : substr($filename, $dot);
    $i = 1;
    while (true) {
        $candidate = $directory . '/' . $stem . '_' . $i . $ext;
        if (!file_exists($candidate)) {
            return $candidate;
        }
        $i++;
    }
}

function path_to_rel(string $abs_path, string $base_dir): string {
    $rel = str_replace('\\', '/', substr($abs_path, strlen($base_dir) + 1));
    return './' . ltrim($rel, '/');
}

function rel_to_abs(string $rel_path, string $base_dir): string {
    $normalized = str_replace('\\', '/', $rel_path);
    $normalized = ltrim($normalized, './');
    return $base_dir . '/' . $normalized;
}

function allowed_extension(string $filename, string $mime): string {
    $ext = strtolower((string)pathinfo($filename, PATHINFO_EXTENSION));
    $allowed = ['jpg', 'jpeg', 'png', 'webp'];
    if (in_array($ext, $allowed, true)) {
        return $ext;
    }
    $mime_map = [
        'image/jpeg' => 'jpg',
        'image/png' => 'png',
        'image/webp' => 'webp',
    ];
    return $mime_map[$mime] ?? 'jpg';
}

function flatten_png_to_white(string $tmp_file, string $dest_file): bool {
    if (!function_exists('imagecreatefrompng') || !function_exists('imagepng')) {
        return false;
    }
    $src = @imagecreatefrompng($tmp_file);
    if ($src === false) {
        return false;
    }
    $w = imagesx($src);
    $h = imagesy($src);
    $dst = imagecreatetruecolor($w, $h);
    $white = imagecolorallocate($dst, 255, 255, 255);
    imagefilledrectangle($dst, 0, 0, $w, $h, $white);
    imagealphablending($dst, true);
    imagesavealpha($dst, false);
    imagecopy($dst, $src, 0, 0, 0, 0, $w, $h);
    $ok = imagepng($dst, $dest_file);
    imagedestroy($src);
    imagedestroy($dst);
    return (bool)$ok;
}

function save_uploaded_image(array $file, string $category, string $photos_dir, string $base_dir): string {
    if (!isset($file['error']) || (int)$file['error'] !== UPLOAD_ERR_OK) {
        respond(['ok' => false, 'error' => 'アップロードエラーが発生しました。'], 400);
    }
    if (!isset($file['tmp_name']) || !is_uploaded_file((string)$file['tmp_name'])) {
        respond(['ok' => false, 'error' => 'アップロードファイルを確認できません。'], 400);
    }
    $cat_dir = $photos_dir . '/' . $category;
    if (!is_dir($cat_dir) && !@mkdir($cat_dir, 0775, true)) {
        respond(['ok' => false, 'error' => 'カテゴリフォルダの作成に失敗しました。'], 500);
    }

    $ext = allowed_extension((string)($file['name'] ?? ''), (string)($file['type'] ?? ''));
    $stem = sanitize_filename_stem((string)pathinfo((string)($file['name'] ?? ''), PATHINFO_FILENAME));
    $ts = date('Ymd_His') . '_' . substr((string)microtime(true), -6);
    $file_name = $ts . '_' . $stem . '.' . $ext;
    $dest = unique_file_path($cat_dir, $file_name);

    $tmp = (string)$file['tmp_name'];
    $saved = false;
    if ($ext === 'png') {
        $saved = flatten_png_to_white($tmp, $dest);
    }
    if (!$saved) {
        $saved = @move_uploaded_file($tmp, $dest);
    }
    if (!$saved) {
        respond(['ok' => false, 'error' => '画像の保存に失敗しました。'], 500);
    }
    return path_to_rel($dest, $base_dir);
}

function move_record_image(string $old_rel, string $new_category, string $photos_dir, string $base_dir): string {
    $old_abs = rel_to_abs($old_rel, $base_dir);
    if (!file_exists($old_abs)) {
        return $old_rel;
    }
    $new_dir = $photos_dir . '/' . $new_category;
    if (!is_dir($new_dir) && !@mkdir($new_dir, 0775, true)) {
        respond(['ok' => false, 'error' => '移動先フォルダ作成に失敗しました。'], 500);
    }
    $base_name = basename($old_abs);
    $new_abs = unique_file_path($new_dir, $base_name);
    if (!@rename($old_abs, $new_abs)) {
        return $old_rel;
    }
    return path_to_rel($new_abs, $base_dir);
}

function delete_record_image(string $rel_path, string $base_dir): void {
    if ($rel_path === '') {
        return;
    }
    $abs = rel_to_abs($rel_path, $base_dir);
    if (is_file($abs)) {
        @unlink($abs);
    }
}

function get_post_tags(): array {
    $tags = $_POST['tags'] ?? [];
    if (!is_array($tags)) {
        return [];
    }
    $values = [];
    foreach ($tags as $t) {
        $values[] = (string)$t;
    }
    return $values;
}

function validate_common_fields(string $category, string $title, array $tags): void {
    if ($category === '') {
        respond(['ok' => false, 'error' => 'アイテムの種類を入力してください。'], 400);
    }
    if (zenkaku_len($category) > 20) {
        respond(['ok' => false, 'error' => '種類は20文字以内です。'], 400);
    }
    if ($title === '') {
        respond(['ok' => false, 'error' => '写真タイトルは必須です。'], 400);
    }
    if (zenkaku_len($title) > 40) {
        respond(['ok' => false, 'error' => 'タイトルは40文字以内です。'], 400);
    }
    foreach ($tags as $tag) {
        if (zenkaku_len(trim((string)$tag)) > 30) {
            respond(['ok' => false, 'error' => '各タグは30文字以内です。'], 400);
        }
    }
}

$action = (string)($_GET['action'] ?? $_POST['action'] ?? '');

if ($_SERVER['REQUEST_METHOD'] === 'GET' && $action === 'list') {
    $records = load_records($DATA_JS);
    $categories = collect_categories($records, $PHOTOS_DIR);
    respond([
        'ok' => true,
        'records' => $records,
        'categories' => $categories,
    ]);
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && $action === 'create') {
    $records = load_records($DATA_JS);
    $category = sanitize_folder_name((string)($_POST['category'] ?? ''));
    $title = trim((string)($_POST['title'] ?? ''));
    $tags = get_post_tags();
    validate_common_fields($category, $title, $tags);
    $description = build_description_from_tags($tags);

    $files = $_FILES['images'] ?? null;
    if (!$files || !isset($files['name'])) {
        respond(['ok' => false, 'error' => '画像が選択されていません。'], 400);
    }

    $created = [];
    $is_multi = is_array($files['name']);
    $count = $is_multi ? count($files['name']) : 1;
    if ($count <= 0) {
        respond(['ok' => false, 'error' => '画像が選択されていません。'], 400);
    }

    for ($i = 0; $i < $count; $i++) {
        $file = [
            'name' => $is_multi ? (string)$files['name'][$i] : (string)$files['name'],
            'type' => $is_multi ? (string)$files['type'][$i] : (string)$files['type'],
            'tmp_name' => $is_multi ? (string)$files['tmp_name'][$i] : (string)$files['tmp_name'],
            'error' => $is_multi ? (int)$files['error'][$i] : (int)$files['error'],
            'size' => $is_multi ? (int)$files['size'][$i] : (int)$files['size'],
        ];
        if ($file['error'] !== UPLOAD_ERR_OK) {
            continue;
        }
        $saved_rel = save_uploaded_image($file, $category, $PHOTOS_DIR, $BASE_DIR);
        $rec_id = date('YmdHis') . str_pad((string)random_int(0, 999999), 6, '0', STR_PAD_LEFT);
        $row = [
            'id' => $rec_id,
            'category' => $category,
            'title' => $title,
            'description' => $description,
            'path' => $saved_rel,
            'ocr_text' => '',
        ];
        $records[] = $row;
        $created[] = $row;
    }

    if (count($created) === 0) {
        respond(['ok' => false, 'error' => '画像の登録に失敗しました。'], 400);
    }

    write_records($DATA_JS, $records);
    respond([
        'ok' => true,
        'message' => count($created) . '件を登録しました。',
        'created' => $created,
    ]);
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && $action === 'update') {
    $records = load_records($DATA_JS);
    $id = (string)($_POST['id'] ?? '');
    if ($id === '') {
        respond(['ok' => false, 'error' => 'IDがありません。'], 400);
    }

    $target_index = -1;
    foreach ($records as $idx => $row) {
        if ((string)($row['id'] ?? '') === $id) {
            $target_index = $idx;
            break;
        }
    }
    if ($target_index < 0) {
        respond(['ok' => false, 'error' => '対象データが見つかりません。'], 404);
    }

    $category = sanitize_folder_name((string)($_POST['category'] ?? ''));
    $title = trim((string)($_POST['title'] ?? ''));
    $tags = get_post_tags();
    validate_common_fields($category, $title, $tags);
    $description = build_description_from_tags($tags);

    $current = $records[$target_index];
    $updated_path = (string)($current['path'] ?? '');
    $replacement = $_FILES['image'] ?? null;
    if (is_array($replacement) && isset($replacement['error']) && (int)$replacement['error'] === UPLOAD_ERR_OK) {
        $new_rel = save_uploaded_image($replacement, $category, $PHOTOS_DIR, $BASE_DIR);
        delete_record_image($updated_path, $BASE_DIR);
        $updated_path = $new_rel;
    } elseif ($category !== (string)($current['category'] ?? '')) {
        $updated_path = move_record_image($updated_path, $category, $PHOTOS_DIR, $BASE_DIR);
    }

    $records[$target_index]['category'] = $category;
    $records[$target_index]['title'] = $title;
    $records[$target_index]['description'] = $description;
    $records[$target_index]['path'] = $updated_path;
    if (!isset($records[$target_index]['ocr_text'])) {
        $records[$target_index]['ocr_text'] = '';
    }

    write_records($DATA_JS, $records);
    respond([
        'ok' => true,
        'message' => '更新しました。',
        'record' => $records[$target_index],
    ]);
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && $action === 'delete') {
    $records = load_records($DATA_JS);
    $id = (string)($_POST['id'] ?? '');
    if ($id === '') {
        respond(['ok' => false, 'error' => 'IDがありません。'], 400);
    }

    $next = [];
    $deleted = null;
    foreach ($records as $row) {
        if ((string)($row['id'] ?? '') === $id) {
            $deleted = $row;
            continue;
        }
        $next[] = $row;
    }
    if ($deleted === null) {
        respond(['ok' => false, 'error' => '対象データが見つかりません。'], 404);
    }
    delete_record_image((string)($deleted['path'] ?? ''), $BASE_DIR);
    write_records($DATA_JS, $next);
    respond(['ok' => true, 'message' => '削除しました。']);
}

respond(['ok' => false, 'error' => '不正なリクエストです。'], 400);

