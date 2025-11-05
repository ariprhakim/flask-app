from flask import Flask, request, jsonify
from flask_cors import CORS
import hashlib
import re
import pdfplumber
import os
import sqlite3
import fitz
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

UPLOAD_FOLDER = "upload"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

DB_PATH = 'plagiarism.db'

# --- Inisialisasi Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS hasil_cek (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            doc1_name TEXT,
            doc2_name TEXT,
            doc1_text TEXT,
            doc2_text TEXT,
            similarity REAL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- Simpan hasil ke DB ---
def save_result_to_db(session_id, doc1_name, doc2_name, doc1_text, doc2_text, similarity):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO hasil_cek 
        (session_id, doc1_name, doc2_name, doc1_text, doc2_text, similarity) 
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (session_id, doc1_name, doc2_name, doc1_text, doc2_text, similarity))
    conn.commit()
    conn.close()

def winnowing_fingerprint(text, k, window_size):
    text = text.lower()
    text = re.sub(r'[^a-z\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    text_for_shingles = text.replace(' ', '')

    if len(text_for_shingles) < k:
        print(f"Panjang teks untuk shingle ({len(text_for_shingles)}) lebih kecil dari k ({k}) -> tidak ada shingle")
        return []

    shingles = [text_for_shingles[i:i+k] for i in range(len(text_for_shingles) - k + 1)]
   
    hashes = [hashlib.sha256(shingle.encode('utf-8')).hexdigest() for shingle in shingles]
  
    fingerprints = []
    for i in range(len(hashes) - window_size + 1):
        window = hashes[i:i+window_size]
        min_hash = min(window)
        fingerprints.append(min_hash)
     
    return fingerprints

def compare_documents(doc1, doc2, k, window_size):
    f1 = winnowing_fingerprint(doc1, k, window_size)
    f2 = winnowing_fingerprint(doc2, k, window_size)
    
    set_f1 = set(f1)
    set_f2 = set(f2)
    
    common_fingerprints = set_f1 & set_f2
    union_fingerprints = set_f1 | set_f2
    
    if not set_f1 or not set_f2:
        print("Salah satu dokumen tidak memiliki fingerprint → similarity = 0.0%")
        return 0.0
    
    similarity = len(common_fingerprints) / len(union_fingerprints) * 100

    return similarity

# --- Ekstrak teks PDF ---
@app.route('/extract-text', methods=['POST'])
def extract_text():
    if 'pdf' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    pdf_file = request.files['pdf']
    try:
        doc = fitz.open(stream=pdf_file.read(), filetype='pdf')
        text = ""
        for page in doc:
            text += page.get_text()
        return jsonify({'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Endpoint untuk deteksi plagiarisme ---
@app.route('/plagiarism', methods=['POST'])
def detect_plagiarism():
    data = request.json
    documents = data['documents']
    k = data['k']
    window_size = data['window_size']

    similarities = []
    session_id = datetime.now(ZoneInfo("Asia/Makassar")).isoformat()
    for i in range(len(documents)):
        for j in range(i + 1, len(documents)):
            doc1 = documents[i]
            doc2 = documents[j]

            similarity = compare_documents(doc1['text'], doc2['text'], k, window_size)

            result = {
                'doc1_index': i,
                'doc2_index': j,
                'doc1_name': doc1['name'],
                'doc2_name': doc2['name'],
                'similarity': similarity,
                'session_id': session_id
            }
            similarities.append(result)

            save_result_to_db(
                session_id=session_id,
                doc1_name=doc1['name'],
                doc2_name=doc2['name'],
                doc1_text=doc1['text'],
                doc2_text=doc2['text'],
                similarity=similarity
            )

    return jsonify({'similarities': similarities, 'session_id': session_id})

# --- Endpoint Riwayat, dikelompokkan berdasarkan sesi ---
@app.route('/history', methods=['GET'])
def get_history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM hasil_cek ORDER BY checked_at DESC')
    rows = c.fetchall()
    conn.close()

    sessions = {}
    for row in rows:
        session_id = row['session_id']
        if session_id not in sessions:
            sessions[session_id] = {
                'session_id': session_id,
                'checked_at': row['checked_at'],
                'results': []
            }
        sessions[session_id]['results'].append({
            'id': row['id'],
            'doc1_name': row['doc1_name'],
            'doc2_name': row['doc2_name'],
            'similarity': row['similarity']
        })

    history = list(sessions.values())
    history.sort(key=lambda x: x['session_id'], reverse=True)

    return jsonify({'history': history})

# --- Ambil isi dokumen dari riwayat ---
@app.route('/history-doc/<int:doc_id>/<string:doc_type>', methods=['GET'])
def get_history_doc(doc_id, doc_type):
    if doc_type not in ['doc1', 'doc2']:
        return jsonify({'error': 'Tipe dokumen harus doc1 atau doc2'}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT id, doc1_name, doc2_name, doc1_text, doc2_text FROM hasil_cek WHERE id = ?', (doc_id,))
        row = c.fetchone()
        if not row:
            return jsonify({'error': 'Dokumen tidak ditemukan'}), 404
    except Exception as e:
        return jsonify({'error': f'Gagal mengambil data: {str(e)}'}), 500
    finally:
        conn.close()

    if doc_type == 'doc1':
        result = {
            'id': row['id'],
            'name': row['doc1_name'],
            'text': row['doc1_text']
        }
    else:
        result = {
            'id': row['id'],
            'name': row['doc2_name'],
            'text': row['doc2_text']
        }

    return jsonify({'dokumen': result})

# --- Endpoint Menghapus Sesi ---
@app.route('/delete-session/<string:session_id>', methods=['DELETE'])
def delete_session(session_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM hasil_cek WHERE session_id = ?', (session_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()

        if deleted == 0:
            return jsonify({'message': 'Tidak ada data dengan session_id tersebut'}), 404

        return jsonify({'message': f'{deleted} data dari session {session_id} berhasil dihapus'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Menjalankan server ---
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)