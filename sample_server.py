from pathlib import Path
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
UPLOAD_DIR = Path('server_uploads')
UPLOAD_DIR.mkdir(exist_ok=True)


@app.post('/upload')
def upload():
    auth = request.headers.get('Authorization', '')
    if auth != 'Bearer change-me':
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'file missing'}), 400

    store_name = request.form.get('store_name', 'Store01')
    employee_name = request.form.get('employee_name', 'unknown')
    camera_id = request.form.get('camera_id', 'camera')
    target_dir = UPLOAD_DIR / store_name / employee_name / camera_id / datetime.now().strftime('%Y-%m-%d')
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(f.filename)
    out_path = target_dir / filename
    f.save(out_path)

    return jsonify({'ok': True, 'remote_id': str(out_path)})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=True)
