# -*- coding: utf-8 -*-
import os
import sqlite3
import json
import csv
import io
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_secret_key_123')
DB_NAME = 'database.db'

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#6c757d',
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category_id INTEGER,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            priority TEXT DEFAULT 'Средний',
            due_date TEXT,
            done INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE SET NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def create_default_categories(user_id):
    conn = get_db_connection()
    count = conn.execute('SELECT COUNT(*) FROM categories WHERE user_id = ?', (user_id,)).fetchone()[0]
    if count == 0:
        defaults = [('Личное', '#0d6efd'), ('Учёба', '#198754'), ('Работа', '#ffc107')]
        for name, color in defaults:
            conn.execute('INSERT INTO categories (user_id, name, color) VALUES (?, ?, ?)', (user_id, name, color))
        conn.commit()
    conn.close()

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    create_default_categories(user_id)

    query = request.args.get('q', '').strip()
    sort_by = request.args.get('sort', 'created_desc')
    filter_type = request.args.get('filter', 'all')
    category_filter = request.args.get('category_id', '')
    page = int(request.args.get('page', 1))
    per_page = 6

    conn = get_db_connection()
    categories = conn.execute('SELECT * FROM categories WHERE user_id = ?', (user_id,)).fetchall()

    sql = '''
        SELECT t.*, c.name as category_name, c.color as category_color 
        FROM tasks t 
        LEFT JOIN categories c ON t.category_id = c.id 
        WHERE t.user_id = ?
    '''
    params = [user_id]

    if filter_type == 'active':
        sql += ' AND t.done = 0'
    elif filter_type == 'completed':
        sql += ' AND t.done = 1'

    if category_filter:
        sql += ' AND t.category_id = ?'
        params.append(category_filter)

    if query:
        sql += ' AND (LOWER(t.title) LIKE LOWER(?) OR LOWER(t.description) LIKE LOWER(?))'
        params.extend([f'%{query}%', f'%{query}%'])

    if sort_by == 'created_asc':
        sql += ' ORDER BY t.id ASC'
    elif sort_by == 'due_date':
        sql += ' ORDER BY CASE WHEN t.due_date IS NULL OR t.due_date = "" THEN 1 ELSE 0 END, t.due_date ASC'
    else:
        sql += ' ORDER BY t.id DESC'

    count_sql = f"SELECT COUNT(*) FROM ({sql})"
    total_tasks = conn.execute(count_sql, params).fetchone()[0]
    total_pages = (total_tasks + per_page - 1) // per_page

    offset = (page - 1) * per_page
    sql += f' LIMIT {per_page} OFFSET {offset}'

    tasks_raw = conn.execute(sql, params).fetchall()
    
    tasks = []
    today_str = datetime.now().strftime('%Y-%m-%d')

    for t in tasks_raw:
        task_dict = dict(t)
        subtasks = conn.execute('SELECT * FROM subtasks WHERE task_id = ?', (t['id'],)).fetchall()
        task_dict['subtasks'] = [dict(s) for s in subtasks]
        
        task_dict['is_overdue'] = False
        if task_dict['due_date'] and not task_dict['done']:
            if task_dict['due_date'] < today_str:
                task_dict['is_overdue'] = True

        tasks.append(task_dict)

    stats = conn.execute('''
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN done = 1 THEN 1 ELSE 0 END) as completed
        FROM tasks WHERE user_id = ?
    ''', (user_id,)).fetchone()

    conn.close()
    return render_template(
        'index.html', 
        tasks=tasks, 
        stats=stats, 
        categories=categories,
        current_filter=filter_type, 
        query=query, 
        sort_by=sort_by,
        category_filter=category_filter,
        page=page,
        total_pages=total_pages
    )

# --- АВТОРИЗАЦИЯ ---
@app.route('/register', methods=('GET', 'POST'))
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        conn = get_db_connection()
        error = None

        if not username or not password:
            error = 'Заполните все поля'
        elif conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone() is not None:
            error = 'Пользователь с таким именем уже существует'

        if error is None:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', 
                           (username, generate_password_hash(password)))
            user_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            # Создаём базовые категории при регистрации
            create_default_categories(user_id)
            flash('Регистрация успешна! Войдите в аккаунт.')
            return redirect(url_for('login'))
        flash(error)
        conn.close()
    return render_template('register.html')

@app.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()

        if user is None or not check_password_hash(user['password'], password):
            flash('Неверное имя пользователя или пароль')
        else:
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- ЗАДАЧИ И РЕДАКТИРОВАНИЕ ---
@app.route('/add', methods=('POST',))
def add_task():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    category_id = request.form.get('category_id') or None
    priority = request.form.get('priority', 'Средний')
    due_date = request.form.get('due_date', '')

    if title:
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO tasks (user_id, category_id, title, description, priority, due_date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session['user_id'], category_id, title, description, priority, due_date))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/edit/<int:task_id>', methods=['POST'])
def edit_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    category_id = request.form.get('category_id') or None
    priority = request.form.get('priority', 'Средний')
    due_date = request.form.get('due_date', '')

    if title:
        conn = get_db_connection()
        conn.execute('''
            UPDATE tasks 
            SET title = ?, description = ?, category_id = ?, priority = ?, due_date = ?
            WHERE id = ? AND user_id = ?
        ''', (title, description, category_id, priority, due_date, task_id, session['user_id']))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/toggle/<int:task_id>')
def toggle_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    task = conn.execute('SELECT * FROM tasks WHERE id = ? AND user_id = ?', (task_id, session['user_id'])).fetchone()
    if task:
        new_status = 0 if task['done'] else 1
        conn.execute('UPDATE tasks SET done = ? WHERE id = ?', (new_status, task_id))
        conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete/<int:task_id>')
def delete_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute('DELETE FROM tasks WHERE id = ? AND user_id = ?', (task_id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

# --- ПОДЗАДАЧИ ---
@app.route('/task/<int:task_id>/subtask/add', methods=['POST'])
def add_subtask(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    title = request.form.get('subtask_title', '').strip()
    if title:
        conn = get_db_connection()
        conn.execute('INSERT INTO subtasks (task_id, title) VALUES (?, ?)', (task_id, title))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/subtask/toggle/<int:subtask_id>')
def toggle_subtask(subtask_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    st = conn.execute('SELECT * FROM subtasks WHERE id = ?', (subtask_id,)).fetchone()
    if st:
        new_status = 0 if st['done'] else 1
        conn.execute('UPDATE subtasks SET done = ? WHERE id = ?', (new_status, subtask_id))
        conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/subtask/delete/<int:subtask_id>')
def delete_subtask(subtask_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute('DELETE FROM subtasks WHERE id = ?', (subtask_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

# --- КАТЕГОРИИ ---
@app.route('/categories', methods=['GET', 'POST'])
def manage_categories():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    conn = get_db_connection()

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        color = request.form.get('color', '#6c757d')
        if name:
            conn.execute('INSERT INTO categories (user_id, name, color) VALUES (?, ?, ?)', (user_id, name, color))
            conn.commit()

    categories = conn.execute('SELECT * FROM categories WHERE user_id = ?', (user_id,)).fetchall()
    conn.close()
    return render_template('categories.html', categories=categories)

@app.route('/categories/delete/<int:cat_id>')
def delete_category(cat_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute('DELETE FROM categories WHERE id = ? AND user_id = ?', (cat_id, session['user_id']))
    conn.commit()
    conn.close()
    return redirect(url_for('manage_categories'))

# --- ЭКСПОРТ/ИМПОРТ ---
@app.route('/export/json')
def export_json():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    tasks = conn.execute('SELECT * FROM tasks WHERE user_id = ?', (session['user_id'],)).fetchall()
    data = [dict(t) for t in tasks]
    conn.close()
    
    mem = io.BytesIO()
    mem.write(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    mem.seek(0)
    return send_file(mem, mimetype='application/json', as_attachment=True, download_name='tasks_backup.json')

@app.route('/import', methods=['POST'])
def import_json():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    file = request.files.get('file')
    if file and file.filename.endswith('.json'):
        try:
            data = json.load(file)
            conn = get_db_connection()
            for item in data:
                conn.execute('''
                    INSERT INTO tasks (user_id, title, description, priority, due_date, done)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    session['user_id'], 
                    item.get('title', 'Без названия'), 
                    item.get('description', ''), 
                    item.get('priority', 'Средний'), 
                    item.get('due_date', ''), 
                    item.get('done', 0)
                ))
            conn.commit()
            conn.close()
            flash('Задачи успешно импортированы!')
        except Exception:
            flash('Ошибка при чтении JSON файла.')
            
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
