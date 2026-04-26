from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import os, shutil, base64

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.expanduser("~/storage/shared/workspace")
os.makedirs(BASE_DIR, exist_ok=True)

def safe_path(rel_path):
    abs_path = os.path.abspath(os.path.join(BASE_DIR, rel_path))
    if not abs_path.startswith(os.path.abspath(BASE_DIR)):
        abort(403, description="Доступ запрещён")
    return abs_path

# ═══════════════ РЕКУРСИВНЫЙ ОБХОД ═══════════════
def _walk_dir(root_abs, base_rel, max_depth, current_depth=0):
    """Собирает все файлы и папки внутри root_abs рекурсивно до max_depth."""
    result = []
    if current_depth > max_depth:
        return result
    try:
        entries = os.listdir(root_abs)
    except (FileNotFoundError, PermissionError):
        return result

    for name in entries:
        full = os.path.join(root_abs, name)
        rel = os.path.relpath(full, BASE_DIR).replace('\\', '/')
        try:
            stat = os.stat(full)
        except OSError:
            continue

        if os.path.isdir(full):
            result.append({"path": rel, "type": "dir", "bytes": 0, "mtime": stat.st_mtime})
            # уходим вглубь, если ещё есть лимит
            if current_depth < max_depth:
                result.extend(_walk_dir(full, rel, max_depth, current_depth + 1))
        else:
            result.append({"path": rel, "type": "file", "bytes": stat.st_size, "mtime": stat.st_mtime})
    return result

@app.route('/ws/list')
def list_files():
    path = request.args.get('path', '.')
    max_depth = int(request.args.get('depth', '10'))
    abs_path = safe_path(path)

    if not os.path.isdir(abs_path):
        abort(404, description="Не папка")

    # Теперь для ЛЮБОГО пути отдаём плоский список ВСЕХ файлов внутри до глубины max_depth
    files = _walk_dir(abs_path, path, max_depth)
    return jsonify({"files": files})

# Остальные эндпоинты без изменений
@app.route('/ws/ping')
def ping():
    return jsonify({"ok": True})

@app.route('/ws/read')
def read_file():
    path = request.args['path']
    abs_path = safe_path(path)
    if not os.path.isfile(abs_path):
        abort(404, description="Файл не найден")
    with open(abs_path, 'rb') as f:
        content = f.read()
    return jsonify({"content": base64.b64encode(content).decode(), "encoding": "base64"})

@app.route('/ws/write', methods=['POST'])
def write_file():
    data = request.json
    if not data or 'path' not in data or 'content' not in data:
        abort(400)
    path = safe_path(data['path'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = base64.b64decode(data['content']) if data.get('encoding') == 'base64' else data['content'].encode('utf-8')
    with open(path, 'wb') as f:
        f.write(raw)
    return jsonify({"bytes": len(raw)})

@app.route('/ws/rm', methods=['POST'])
def rm():
    data = request.json
    if not data or 'path' not in data:
        abort(400)
    path = safe_path(data['path'])
    recursive = data.get('recursive', True)
    if os.path.isdir(path):
        shutil.rmtree(path) if recursive else os.rmdir(path)
    else:
        os.remove(path)
    return jsonify({"ok": True})

@app.route('/ws/mkdir', methods=['POST'])
def mkdir():
    path = safe_path(request.json['path'])
    os.makedirs(path, exist_ok=True)
    return jsonify({"ok": True})

@app.route('/ws/move', methods=['POST'])
def move():
    d = request.json
    shutil.move(safe_path(d['src']), safe_path(d['dest']))
    return jsonify({"ok": True})

@app.route('/ws/exists')
def exists():
    try:
        return jsonify({"exists": os.path.exists(safe_path(request.args.get('path', '.')))})
    except:
        return jsonify({"exists": False})

@app.route('/ws/reset', methods=['POST'])
def reset():
    shutil.rmtree(BASE_DIR)
    os.makedirs(BASE_DIR)
    return jsonify({"ok": True})

if __name__ == '__main__':
    print("● Workspace API (рекурсивный) запущен на http://0.0.0.0:8764")
    app.run(host='0.0.0.0', port=8764, debug=False)