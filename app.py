import os
import io
import re
import csv
import secrets
import string
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import datetime
import traceback
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, Response, send_file, send_from_directory, abort, session
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename
import pandas as pd
import threading

from sqlalchemy import event, func
from sqlalchemy.engine import Engine

from models import db, User, Patient, Scan, Trial, CustomData, TrialPatientData, patient_trials, scan_trials
from drive_service import GoogleDriveService, parse_dicom_bytes, DEFAULT_DRIVE_FOLDER_ID, extract_cr_number, clean_patient_name, clean_age, Request
from local_scanner import scan_local_directory, extract_metadata_from_file, is_dicom_file, find_matching_patient
from sample_data import seed_database_and_samples

# Configuration: Primary local filesystem directory for radiology datasets (Google Drive for Desktop mirror)
LOCAL_DRIVE_PATH = r"D:\RADIOLOGY DATA"

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'orthoreg-clinical-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(app.root_path, 'instance', 'orthoreg.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64 MB max upload

# SMTP Email Configuration for User Invitations & Alerts
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'on', '1']
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'false').lower() in ['true', 'on', '1']
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'no-reply@orthoreg.org')

# Enable Write-Ahead Logging (WAL) and busy timeout on SQLite to handle background thread writes
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()

# Global background crawler telemetry state
crawler_state = {
    'is_running': False,
    'current_folder': None,
    'folders_scanned': 0,
    'files_scanned': 0,
    'total_files': 0,
    'current_file_idx': 0,
    'ingested': 0,
    'skipped': 0,
    'last_patient': None,
    'last_error': None,
    'started_at': None
}

# Ensure directories
os.makedirs(os.path.join(app.root_path, 'instance'), exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'custom_files'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'dicom'), exist_ok=True)

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the Orthopedic Radiology Registry.'
login_manager.login_message_category = 'warning'


@login_manager.unauthorized_handler
def handle_unauthorized():
    if request.path.startswith('/api/') or request.path.endswith('/dicom') or '/dicom/' in request.path:
        return Response("Authentication required to access DICOM binary stream", status=401, mimetype='text/plain')
    flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(url_for(login_manager.login_view, next=request.url))


drive_service = GoogleDriveService(root_dir=app.root_path)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def requires_role(*allowed_roles):
    """Enforce Role-Based Access Control on Flask endpoints.
    Hierarchy: Admin > Editor > User
    - If 'Admin' is allowed: only Admin (or legacy PI) has access.
    - If 'Editor' is allowed: Admin and Editor have access.
    - If 'User' is allowed: Admin, Editor, and User have access.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()

            # Map legacy roles
            user_role = current_user.role
            if user_role == 'PI':
                user_role = 'Admin'
            elif user_role == 'Viewer':
                user_role = 'User'

            allowed = set(allowed_roles)
            if 'User' in allowed:
                allowed.update(['Admin', 'Editor'])
            if 'Editor' in allowed:
                allowed.add('Admin')

            if user_role not in allowed and current_user.role not in allowed:
                if request.path.startswith('/api/'):
                    return Response(
                        f"Forbidden: Role '{current_user.role}' lacks permission for this action.",
                        status=403,
                        mimetype='text/plain'
                    )
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# Maintain backward-compatible alias
role_required = requires_role


# ---------------------------------------------------------
# AUTHENTICATION & ACCESS REQUEST ROUTES
# ---------------------------------------------------------

def generate_temp_password(length=8):
    """Generate a secure, random 8-character temporary password with uppercase, lowercase, and digits."""
    chars = string.ascii_letters + string.digits
    while True:
        pwd = ''.join(secrets.choice(chars) for _ in range(length))
        if any(c.isupper() for c in pwd) and any(c.islower() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def send_invitation_email(recipient_email, recipient_name, temp_password, portal_url):
    """Send an HTML email to an invited user containing their temporary password and portal link."""
    sender = app.config.get('MAIL_DEFAULT_SENDER', 'no-reply@orthoreg.org')
    subject = "Your Orthopedic Radiology Registry Account Invitation"

    html_content = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #090d16; color: #f1f5f9; padding: 24px;">
  <div style="max-width: 520px; margin: 0 auto; background: #0f172a; border-radius: 16px; border: 1px solid #334155; padding: 32px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);">
    <div style="text-align: center; margin-bottom: 20px;">
      <h2 style="color: #38bdf8; margin: 0; font-size: 20px;">Orthopedic Radiology Registry</h2>
      <p style="color: #94a3b8; font-size: 12px; margin-top: 4px;">Institutional PACS & Clinical Research Portal</p>
    </div>
    <p style="font-size: 13px; color: #e2e8f0;">Dear {recipient_name or 'Colleague'},</p>
    <p style="font-size: 13px; color: #cbd5e1; line-height: 1.6;">
      You have been granted access to the Orthopedic Radiology Registry. An active user profile has been created for you with a temporary one-time password.
    </p>
    <div style="background-color: #090d16; border-radius: 12px; border: 1px solid #0284c7; padding: 16px; margin: 20px 0; text-align: center;">
      <div style="font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">Temporary One-Time Password</div>
      <div style="font-size: 24px; font-weight: bold; font-family: monospace; color: #38bdf8; letter-spacing: 2px;">{temp_password}</div>
      <div style="font-size: 11px; color: #f43f5e; margin-top: 6px;">* For security, you will be prompted to set a permanent password upon first sign-in.</div>
    </div>
    <div style="text-align: center; margin: 24px 0;">
      <a href="{portal_url}" style="background: linear-gradient(135deg, #06b6d4, #2563eb); color: #020617; font-weight: bold; text-decoration: none; padding: 12px 28px; border-radius: 10px; font-size: 13px; display: inline-block;">
        Sign In to Access Registry
      </a>
    </div>
    <p style="font-size: 11px; color: #64748b; margin-top: 24px; border-top: 1px solid #1e293b; padding-top: 14px;">
      If you did not expect this invitation, please contact your Institutional Review Board or System Administrator.
    </p>
  </div>
</body>
</html>"""

    mail_server = app.config.get('MAIL_SERVER')
    mail_user = app.config.get('MAIL_USERNAME')
    mail_pass = app.config.get('MAIL_PASSWORD')
    mail_port = app.config.get('MAIL_PORT', 587)
    use_tls = app.config.get('MAIL_USE_TLS', True)
    use_ssl = app.config.get('MAIL_USE_SSL', False)

    if not mail_server or not mail_user or not mail_pass:
        print(f"[SMTP Notice] Mail server credentials not configured. Simulated email sent to {recipient_email} | Temp Password: {temp_password} | Link: {portal_url}")
        return True

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = recipient_email
        msg.attach(MIMEText(html_content, 'html'))

        if use_ssl:
            with smtplib.SMTP_SSL(mail_server, mail_port) as server:
                server.login(mail_user, mail_pass)
                server.sendmail(sender, [recipient_email], msg.as_string())
        else:
            with smtplib.SMTP(mail_server, mail_port) as server:
                if use_tls:
                    server.starttls()
                server.login(mail_user, mail_pass)
                server.sendmail(sender, [recipient_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[SMTP Error] Failed to send email to {recipient_email}: {e}")
        return False


@app.route('/login', methods=['GET', 'POST'])
def login():
    is_json = (
        request.is_json or
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        'application/json' in request.headers.get('Accept', '') or
        bool(request.content_type and 'application/json' in request.content_type)
    )

    if current_user.is_authenticated:
        target = url_for('dashboard')
        if is_json:
            return jsonify({'success': True, 'redirect': target})
        return redirect(target)

    if request.method == 'POST':
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            identifier = (payload.get('username') or payload.get('email') or '').strip()
            password = payload.get('password', '')
        else:
            identifier = (request.form.get('username') or request.form.get('email') or '').strip()
            password = request.form.get('password', '')

        user = None
        if identifier:
            user = User.query.filter(
                (func.lower(User.username) == identifier.lower()) |
                (func.lower(User.email) == identifier.lower())
            ).first()

        user_status = (user.status or 'Active').capitalize() if user else None

        if user and user.check_password(password):
            if user_status == 'Pending':
                msg = "Your access request is currently pending administrative approval. You will be able to log in once reviewed."
                if is_json:
                    return jsonify({'success': False, 'error': msg, 'status': 'Pending'}), 403
                flash(msg, "warning")
                return redirect(url_for('login'))
            elif user_status == 'Revoked':
                msg = "Your registry access has been revoked. Please contact the administrator."
                if is_json:
                    return jsonify({'success': False, 'error': msg, 'status': 'Revoked'}), 403
                flash(msg, "danger")
                return redirect(url_for('login'))
            elif user_status == 'Active':
                # First-Time Login Interceptor: If must_change_password is True, do NOT establish standard session
                if bool(user.must_change_password):
                    session['setup_user_id'] = user.id
                    flash("Temporary credentials detected. Please configure your permanent secure password to continue.", "info")
                    target = url_for('setup_password')
                    if is_json:
                        return jsonify({
                            'success': True,
                            'redirect': target,
                            'must_change_password': True
                        })
                    return redirect(target)

                login_user(user)
                flash(f"Welcome back, {user.display_name} ({user.role})", "success")
                next_page = request.args.get('next') or url_for('dashboard')
                if is_json:
                    return jsonify({'success': True, 'redirect': next_page})
                return redirect(next_page)
            else:
                msg = f"Account has status '{user.status}'. Please contact the administrator."
                if is_json:
                    return jsonify({'success': False, 'error': msg}), 403
                flash(msg, "warning")
                return redirect(url_for('login'))
        else:
            msg = "Invalid email/username or password. Please try again."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 401
            flash(msg, "danger")

    return render_template('login.html')


@app.route('/setup-password', methods=['GET', 'POST'])
def setup_password():
    """First-time login view requiring users with temporary passwords to configure a permanent password."""
    setup_uid = session.get('setup_user_id')
    target_user = db.session.get(User, setup_uid) if setup_uid else None

    is_json = (
        request.is_json or
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        'application/json' in request.headers.get('Accept', '') or
        bool(request.content_type and 'application/json' in request.content_type)
    )

    if request.method == 'POST':
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            temp_password = payload.get('temp_password', '')
            new_password = payload.get('new_password', '')
            confirm_password = payload.get('confirm_password', '')
            identifier = (payload.get('identifier') or '').strip().lower()
        else:
            temp_password = request.form.get('temp_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')
            identifier = (request.form.get('identifier') or '').strip().lower()

        if not target_user:
            if not identifier:
                msg = "Please provide your account email or username."
                if is_json:
                    return jsonify({'success': False, 'error': msg}), 400
                flash(msg, "danger")
                return render_template('setup_password.html', target_user=None)
            target_user = User.query.filter(
                (func.lower(User.email) == identifier) |
                (func.lower(User.username) == identifier)
            ).first()

        if not target_user:
            msg = "Account not found. Please verify your email or username."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 404
            flash(msg, "danger")
            return render_template('setup_password.html', target_user=None)

        if not target_user.check_password(temp_password):
            msg = "Current temporary password is incorrect."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 401
            flash(msg, "danger")
            return render_template('setup_password.html', target_user=target_user)

        if len(new_password) < 6:
            msg = "New password must be at least 6 characters long."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, "danger")
            return render_template('setup_password.html', target_user=target_user)

        if new_password != confirm_password:
            msg = "New password and confirmation do not match."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, "danger")
            return render_template('setup_password.html', target_user=target_user)

        if temp_password == new_password:
            msg = "New password cannot be the same as your temporary password."
            if is_json:
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, "warning")
            return render_template('setup_password.html', target_user=target_user)

        # Update password hash, mark status as Active, and clear must_change_password
        target_user.set_password(new_password)
        target_user.must_change_password = False
        target_user.status = 'Active'
        db.session.commit()

        session.pop('setup_user_id', None)
        login_user(target_user)

        flash("Permanent password configured successfully! Welcome to the registry.", "success")
        if is_json:
            return jsonify({'success': True, 'redirect': url_for('dashboard')})
        return redirect(url_for('dashboard'))

    return render_template('setup_password.html', target_user=target_user)


@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Allows active authenticated users to update their password from the profile dropdown."""
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not current_user.check_password(current_password):
            flash("Current password is incorrect.", "danger")
            return render_template('change_password.html')

        if len(new_password) < 6:
            flash("New password must be at least 6 characters long.", "danger")
            return render_template('change_password.html')

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return render_template('change_password.html')

        current_user.set_password(new_password)
        current_user.must_change_password = False
        db.session.commit()

        flash("Password updated successfully.", "success")
        return redirect(url_for('dashboard'))

    return render_template('change_password.html')


@app.route('/request-access', methods=['POST'])
def request_access():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    email = request.form.get('email', '').strip().lower()
    name = request.form.get('name', '').strip()
    password = request.form.get('password', '').strip()
    department = request.form.get('department', '').strip()

    if not email:
        flash("Email address is required to request access.", "danger")
        return redirect(url_for('login') + '?tab=request')

    if not name:
        flash("Full name is required to request access.", "danger")
        return redirect(url_for('login') + '?tab=request')

    if not password or len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for('login') + '?tab=request')

    existing = User.query.filter((User.email == email) | (User.username == email)).first()
    if existing:
        if existing.status == 'Pending':
            flash(f"An access request for '{email}' is already under administrative review.", "warning")
        elif existing.status == 'Active':
            flash(f"An account for '{email}' already exists. Please sign in.", "info")
        else:
            flash(f"An account for '{email}' exists with status '{existing.status}'. Contact an administrator.", "danger")
        return redirect(url_for('login'))

    new_user = User(
        email=email,
        name=name,
        username=email,
        role='User',
        status='Pending'
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()

    flash("Access request submitted successfully! Your account is pending administrative approval.", "success")
    return redirect(url_for('login'))


@app.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    logout_user()
    flash("You have been successfully logged out.", "info")
    return redirect(url_for('login'))


# ---------------------------------------------------------
# ADMIN USER MANAGEMENT PANEL
# ---------------------------------------------------------

@app.route('/admin/users')
@login_required
@requires_role('Admin')
def admin_users():
    pending_users = User.query.filter_by(status='Pending').order_by(User.created_at.desc()).all()
    active_users = User.query.filter(User.status.in_(['Active', 'Revoked'])).order_by(User.created_at.desc()).all()

    stats = {
        'total': User.query.count(),
        'active': User.query.filter_by(status='Active').count(),
        'pending': len(pending_users),
        'admins': User.query.filter(User.role.in_(['Admin', 'PI']), User.status == 'Active').count(),
        'editors': User.query.filter_by(role='Editor', status='Active').count(),
        'users': User.query.filter(User.role.in_(['User', 'Viewer']), User.status == 'Active').count(),
    }

    created_user_creds = session.pop('created_user_creds', None)

    return render_template(
        'admin_users.html',
        pending_users=pending_users,
        active_users=active_users,
        stats=stats,
        created_user_creds=created_user_creds
    )


@app.route('/admin/users/<int:user_id>/approve', methods=['POST'])
@login_required
@requires_role('Admin')
def approve_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    role = request.form.get('role', 'User')
    if role not in ['Admin', 'Editor', 'User']:
        role = 'User'

    user.role = role
    user.status = 'Active'
    user.approved_at = datetime.datetime.now(datetime.timezone.utc)
    db.session.commit()

    flash(f"User '{user.display_email}' approved with role '{role}'.", "success")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/deny', methods=['POST'])
@login_required
@requires_role('Admin')
def deny_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    email = user.display_email
    db.session.delete(user)
    db.session.commit()

    flash(f"Access request for '{email}' denied and removed.", "warning")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/role', methods=['POST'])
@login_required
@requires_role('Admin')
def update_user_role(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    new_role = request.form.get('role')
    if new_role not in ['Admin', 'Editor', 'User']:
        flash("Invalid role specified.", "danger")
        return redirect(url_for('admin_users'))

    if user.id == current_user.id and new_role != 'Admin':
        flash("Security violation: You cannot demote your own active Admin account.", "danger")
        return redirect(url_for('admin_users'))

    user.role = new_role
    db.session.commit()

    flash(f"Role for '{user.display_email}' updated to '{new_role}'.", "success")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/revoke', methods=['POST'])
@login_required
@requires_role('Admin')
def revoke_user_access(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    if user.id == current_user.id:
        flash("Security violation: You cannot revoke your own access.", "danger")
        return redirect(url_for('admin_users'))

    user.status = 'Revoked'
    db.session.commit()

    flash(f"Access for user '{user.display_email}' has been revoked.", "info")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/restore', methods=['POST'])
@login_required
@requires_role('Admin')
def restore_user_access(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    user.status = 'Active'
    db.session.commit()

    flash(f"Access for user '{user.display_email}' has been restored.", "success")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/invite', methods=['POST'])
@login_required
@requires_role('Admin')
def invite_user():
    email = request.form.get('email', '').strip().lower()
    name = request.form.get('name', '').strip()
    role_input = (request.form.get('role', 'User') or 'User').strip().capitalize()
    role = role_input if role_input in ['Admin', 'Editor', 'User'] else 'User'

    if not email:
        flash("Email address is required to invite a user.", "danger")
        return redirect(url_for('admin_users'))

    custom_pwd = (request.form.get('password') or request.form.get('initial_password') or '').strip()
    temp_password = custom_pwd if custom_pwd else generate_temp_password(8)
    portal_url = url_for('login', _external=True)

    existing = User.query.filter((User.email == email) | (User.username == email)).first()
    if existing:
        if existing.status == 'Pending':
            existing.status = 'Active'
            existing.role = role
            if name:
                existing.name = name
            existing.set_password(temp_password)
            existing.must_change_password = True
            existing.approved_at = datetime.datetime.now(datetime.timezone.utc)
            db.session.commit()

            try:
                send_invitation_email(email, name or existing.name, temp_password, portal_url)
            except Exception as e:
                print(f"[SMTP Notice] Invitation email could not be delivered: {e}")

            session['created_user_creds'] = {
                'email': email,
                'name': name or existing.name,
                'role': role,
                'temp_password': temp_password,
                'must_change_password': True
            }
            flash(f"Pending user '{email}' was activated with role '{role}'! Temporary Password: {temp_password} (Share directly with user).", "success")
        else:
            flash(f"User '{email}' already exists with status '{existing.status}'.", "warning")
        return redirect(url_for('admin_users'))

    new_user = User(
        email=email,
        name=name or email.split('@')[0],
        username=email,
        role=role,
        status='Active',
        must_change_password=True,
        approved_at=datetime.datetime.now(datetime.timezone.utc)
    )
    new_user.set_password(temp_password)
    db.session.add(new_user)
    db.session.commit()

    try:
        send_invitation_email(email, name or new_user.name, temp_password, portal_url)
    except Exception as e:
        print(f"[SMTP Notice] Invitation email could not be delivered: {e}")

    session['created_user_creds'] = {
        'email': email,
        'name': name or new_user.name,
        'role': role,
        'temp_password': temp_password,
        'must_change_password': True
    }

    flash(f"User account '{email}' created with role '{role}'! Temporary Password: {temp_password} (Share directly with user).", "success")
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
@requires_role('Admin')
def delete_user(user_id):
    """Safely delete user account from the registry database with root Admin protection."""
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin_users'))

    if user.id == current_user.id:
        flash("Security violation: You cannot delete your own root Admin account.", "danger")
        return redirect(url_for('admin_users'))

    email = user.display_email
    db.session.delete(user)
    db.session.commit()

    flash(f"User account '{email}' has been permanently deleted from the database.", "warning")
    return redirect(url_for('admin_users'))


# ---------------------------------------------------------
# DASHBOARD ROUTE
# ---------------------------------------------------------

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    search_query = (request.args.get('search') or request.args.get('q', '')).strip()
    modality_filter = request.args.get('modality', '').strip()
    trial_filter = request.args.get('trial', '').strip()
    gender_filter = request.args.get('gender', '').strip()
    sort_by = request.args.get('sort_by', '').strip().lower()
    order = request.args.get('order', '').strip().lower()

    # Base query for patients
    query = Patient.query

    # Apply search filter (CR number or patient name)
    if search_query:
        query = query.filter(
            (Patient.cr_number.ilike(f"%{search_query}%")) |
            (Patient.patient_name.ilike(f"%{search_query}%"))
        )

    # Apply gender filter (flexible for M/Male, F/Female, Other)
    if gender_filter:
        if gender_filter.upper() in ('M', 'MALE'):
            query = query.filter((Patient.gender == 'M') | (Patient.gender.ilike('Male%')))
        elif gender_filter.upper() in ('F', 'FEMALE'):
            query = query.filter((Patient.gender == 'F') | (Patient.gender.ilike('Female%')))
        else:
            query = query.filter(Patient.gender.ilike(f"%{gender_filter}%"))

    # Apply trial filter (by trial ID or trial name)
    if trial_filter:
        if trial_filter.isdigit():
            query = query.join(Patient.trials).filter(Trial.id == int(trial_filter))
        else:
            query = query.join(Patient.trials).filter(Trial.trial_name.ilike(f"%{trial_filter}%"))

    # Apply modality filter
    if modality_filter:
        query = query.join(Patient.scans).filter(Scan.modality.ilike(modality_filter))

    # Ensure unique patient rows if joins produced multiples
    query = query.distinct()

    # Column sorting
    if order not in ('asc', 'desc'):
        order = 'asc' if sort_by else 'desc'

    if sort_by == 'cr_number':
        query = query.order_by(Patient.cr_number.desc() if order == 'desc' else Patient.cr_number.asc())
    elif sort_by == 'patient_name':
        query = query.order_by(Patient.patient_name.desc() if order == 'desc' else Patient.patient_name.asc())
    elif sort_by in ('demographics', 'age'):
        if order == 'desc':
            query = query.order_by(Patient.age.desc(), Patient.gender.desc())
        else:
            query = query.order_by(Patient.age.asc(), Patient.gender.asc())
    else:
        # Default sorting by created_at descending
        sort_by = ''
        order = 'desc'
        query = query.order_by(Patient.created_at.desc())

    patients = query.all()
    all_trials = Trial.query.order_by(Trial.trial_name).all()

    # Aggregate Statistics
    total_patients = Patient.query.count()
    total_scans = Scan.query.count()
    total_trials = Trial.query.count()

    modalities = [r[0] for r in db.session.query(Scan.modality).distinct() if r[0]]

    # Helper function to generate sorting URLs preserving active filter parameters
    def make_sort_url(column):
        next_order = 'desc' if (sort_by == column and order == 'asc') else 'asc'
        params = {}
        if search_query:
            params['search'] = search_query
        if modality_filter:
            params['modality'] = modality_filter
        if trial_filter:
            params['trial'] = trial_filter
        if gender_filter:
            params['gender'] = gender_filter
        params['sort_by'] = column
        params['order'] = next_order
        return url_for('dashboard', **params)

    return render_template(
        'dashboard.html',
        patients=patients,
        all_trials=all_trials,
        modalities=modalities,
        total_patients=total_patients,
        total_scans=total_scans,
        total_trials=total_trials,
        selected_search=search_query,
        selected_modality=modality_filter,
        selected_trial=trial_filter,
        selected_gender=gender_filter,
        sort_by=sort_by,
        order=order,
        make_sort_url=make_sort_url,
        is_drive_configured=drive_service.is_configured()
    )


# ---------------------------------------------------------
# PATIENT METADATA EDITING & CUSTOM DATA (ADMIN / PI ONLY)
# ---------------------------------------------------------

@app.route('/patient/edit', methods=['POST'])
@app.route('/patient/edit/', methods=['POST'])
@app.route('/patient/edit/<cr_number>', methods=['POST'])
@app.route('/patient/<cr_number>/edit', methods=['POST'])
@login_required
@requires_role('Admin', 'Editor')
def edit_patient(cr_number=None):
    json_data = request.get_json(silent=True) if request.is_json else {}
    if not cr_number:
        cr_number = request.form.get('cr_number', '').strip()
        if not cr_number and json_data:
            cr_number = str(json_data.get('cr_number') or '').strip()

    patient = db.session.get(Patient, cr_number) if cr_number else None
    if not patient:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': f"Patient '{cr_number}' not found."}), 404
        flash("Patient not found.", "danger")
        return redirect(url_for('dashboard'))

    def has_field(k):
        return k in request.form or (bool(json_data) and k in json_data)

    def get_field_val(k):
        if k in request.form:
            return request.form.get(k, '').strip()
        if json_data and k in json_data:
            v = json_data.get(k)
            return str(v).strip() if v is not None else ''
        return ''

    if has_field('patient_name') or has_field('name'):
        p_name = get_field_val('patient_name') or get_field_val('name')
        if p_name:
            patient.patient_name = p_name
    if has_field('age'):
        p_age = get_field_val('age')
        if p_age:
            patient.age = p_age
    if has_field('gender'):
        p_gender = get_field_val('gender')
        if p_gender:
            patient.gender = p_gender

    if has_field('diagnosis'):
        patient.diagnosis = get_field_val('diagnosis') or None

    if has_field('ao_grade') or has_field('ao_trauma_score'):
        ao_val = get_field_val('ao_grade') or get_field_val('ao_trauma_score')
        patient.ao_grade = ao_val or None
        patient.ao_trauma_score = ao_val or None

    if has_field('anatomy'):
        patient.anatomy = get_field_val('anatomy') or None

    if has_field('scanogram'):
        scano_val = get_field_val('scanogram')
        if scano_val.lower() in ('yes', 'on', 'true', '1'):
            patient.scanogram = 'Yes'
        elif scano_val.lower() in ('no', 'false', '0'):
            patient.scanogram = 'No'
        elif scano_val:
            patient.scanogram = scano_val
        else:
            patient.scanogram = 'No'

    db.session.commit()

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'message': f"Clinical profile for Patient {patient.cr_number} updated successfully.",
            'patient': {
                'cr_number': patient.cr_number,
                'patient_name': patient.patient_name,
                'diagnosis': patient.diagnosis,
                'ao_grade': patient.ao_grade,
                'anatomy': patient.anatomy,
                'scanogram': patient.scanogram
            }
        })

    flash(f"Clinical profile for Patient {patient.cr_number} updated successfully.", "success")
    return redirect(url_for('dashboard'))


@app.route('/patient/<cr_number>/assign_trials', methods=['POST'])
@login_required
@requires_role('Editor')
def assign_patient_trials(cr_number):
    patient = db.session.get(Patient, cr_number)
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for('dashboard'))

    selected_trial_ids = request.form.getlist('trial_ids')
    trials = Trial.query.filter(Trial.id.in_([int(tid) for tid in selected_trial_ids])).all() if selected_trial_ids else []
    
    patient.trials = trials
    # Also associate scans of this patient with selected trials
    for scan in patient.scans:
        scan.trials = trials

    db.session.commit()
    flash(f"Assigned {len(trials)} trial(s) to Patient {patient.cr_number}.", "success")
    return redirect(url_for('dashboard'))


@app.route('/patient/<cr_number>/custom_data', methods=['POST'])
@login_required
@requires_role('Editor')
def add_custom_data(cr_number):
    patient = db.session.get(Patient, cr_number)
    if not patient:
        flash("Patient not found.", "danger")
        return redirect(url_for('dashboard'))

    field_name = request.form.get('field_name', '').strip()
    field_type = request.form.get('field_type', 'text').strip()  # 'text', 'link', 'file'

    if not field_name:
        flash("Field name is required.", "danger")
        return redirect(url_for('dashboard'))

    field_value = ''
    if field_type == 'file':
        uploaded_file = request.files.get('file_upload')
        if uploaded_file and uploaded_file.filename:
            fname = secure_filename(uploaded_file.filename)
            unique_fname = f"{cr_number}_{int(datetime.datetime.utcnow().timestamp())}_{fname}"
            dest_path = os.path.join(app.config['UPLOAD_FOLDER'], 'custom_files', unique_fname)
            uploaded_file.save(dest_path)
            field_value = f"/uploads/custom_files/{unique_fname}"
        else:
            flash("No file was uploaded for the custom file field.", "danger")
            return redirect(url_for('dashboard'))
    elif field_type == 'link':
        field_value = request.form.get('field_link', '').strip()
    else:
        field_value = request.form.get('field_text', '').strip()

    custom_entry = CustomData(
        cr_number=patient.cr_number,
        field_name=field_name,
        field_type=field_type,
        field_value=field_value
    )
    db.session.add(custom_entry)
    db.session.commit()

    flash(f"Added custom column '{field_name}' to Patient {patient.cr_number}.", "success")
    return redirect(url_for('dashboard'))


@app.route('/uploads/custom_files/<filename>')
@login_required
def view_custom_file(filename):
    custom_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'custom_files')
    return send_from_directory(custom_dir, secure_filename(filename))


@app.route('/patient/<cr_number>/delete', methods=['POST'])
@login_required
@requires_role('Admin')
def delete_patient(cr_number):
    patient = db.session.get(Patient, cr_number)
    if patient:
        db.session.delete(patient)
        db.session.commit()
        flash(f"Patient {cr_number} and all associated scans removed.", "info")
    return redirect(url_for('dashboard'))


@app.route('/patient/register', methods=['POST'])
@login_required
@requires_role('Editor')
def register_patient():
    """Manual Pre-Scan Registration: Create a valid Patient record with 0 associated scans."""
    raw_cr = request.form.get('cr_number', '').strip()
    raw_name = request.form.get('patient_name', '').strip()
    raw_age = request.form.get('age', '').strip()
    gender = request.form.get('gender', 'Other').strip()
    diagnosis = request.form.get('diagnosis', '').strip() or None
    ao_grade = (request.form.get('ao_grade') or request.form.get('ao_trauma_score') or '').strip() or None
    anatomy = request.form.get('anatomy', '').strip() or None
    raw_scanogram = request.form.get('scanogram', '').strip()
    scanogram = 'Yes' if raw_scanogram.lower() in ('yes', 'on', 'true', '1') else 'No'
    selected_trials = request.form.getlist('trial_ids')

    cr_num = extract_cr_number(raw_cr, raw_name)
    if not cr_num or cr_num in ('CR-UNKNOWN', 'UNKNOWN'):
        if raw_cr:
            clean_raw = raw_cr.upper().replace('CR', '').replace('-', '').strip()
            cr_num = f"CR{clean_raw}" if clean_raw else f"CR{int(datetime.datetime.now().timestamp())}"
        else:
            cr_num = f"CR{int(datetime.datetime.now().timestamp())}"

    existing = db.session.get(Patient, cr_num)
    if existing:
        flash(f"Patient with CR Number '{cr_num}' already exists in registry.", "warning")
        return redirect(url_for('dashboard', search=cr_num))

    patient_name = clean_patient_name(raw_name) if raw_name else 'Pre-Scan Registered Patient'
    age = clean_age(raw_age) if raw_age else 'Unknown'

    patient = Patient(
        cr_number=cr_num,
        patient_name=patient_name,
        age=age,
        gender=gender,
        diagnosis=diagnosis,
        ao_trauma_score=ao_grade,
        ao_grade=ao_grade,
        anatomy=anatomy,
        scanogram=scanogram
    )

    if selected_trials:
        for t_id in selected_trials:
            try:
                t = db.session.get(Trial, int(t_id))
                if t and t not in patient.trials:
                    patient.trials.append(t)
            except Exception:
                pass

    db.session.add(patient)
    db.session.commit()

    flash(f"Patient '{patient.patient_name}' (CR: {patient.cr_number}) successfully registered (Pre-Scan mode: 0 scans).", "success")
    return redirect(url_for('dashboard', search=patient.cr_number))


# ---------------------------------------------------------
# CURATE TRIAL VIEW & EDC SEMANTIC FIELD MAPPING
# ---------------------------------------------------------

# Semantic mapping linking common dynamic EDC field names to standard Patient model attributes
PATIENT_SEMANTIC_FIELD_MAP = {
    # CR Number variations
    'cr_no': 'cr_number',
    'cr no': 'cr_number',
    'cr no.': 'cr_number',
    'cr number': 'cr_number',
    'cr_number': 'cr_number',
    'cr': 'cr_number',
    'cr_num': 'cr_number',
    'crnum': 'cr_number',
    'patient_cr': 'cr_number',
    'patient cr': 'cr_number',
    'patient_cr_no': 'cr_number',
    'patient cr no': 'cr_number',
    'patient_cr_number': 'cr_number',
    'patient cr number': 'cr_number',
    'central registration number': 'cr_number',
    'central registration no': 'cr_number',

    # Name variations
    'name': 'name',
    'patient_name': 'name',
    'patient name': 'name',
    'pt_name': 'name',
    'pt name': 'name',
    'full_name': 'name',
    'full name': 'name',
    'patient full name': 'name',

    # Age variations
    'age': 'age',
    'patient_age': 'age',
    'patient age': 'age',
    'pt_age': 'age',
    'pt age': 'age',

    # Gender / Sex variations
    'gender': 'gender',
    'sex': 'gender',
    'patient_gender': 'gender',
    'patient gender': 'gender',
    'patient_sex': 'gender',
    'patient sex': 'gender',
    'pt_gender': 'gender',
    'pt sex': 'gender',

    # Diagnosis variations
    'diagnosis': 'diagnosis',
    'provisional_diagnosis': 'diagnosis',
    'provisional diagnosis': 'diagnosis',
    'clinical_diagnosis': 'diagnosis',
    'clinical diagnosis': 'diagnosis',
    'dx': 'diagnosis',

    # AO Trauma Score & Grade variations
    'ao_trauma_score': 'ao_trauma_score',
    'ao trauma score': 'ao_trauma_score',
    'ao_grade': 'ao_grade',
    'ao grade': 'ao_grade',
    'ao_score': 'ao_trauma_score',
    'ao score': 'ao_trauma_score',
    'ao_classification': 'ao_trauma_score',
    'ao classification': 'ao_trauma_score',
    'ao/ota_classification': 'ao_trauma_score',
    'ao/ota classification': 'ao_trauma_score',
    'ao': 'ao_trauma_score',

    # Anatomy variations
    'anatomy': 'anatomy',
    'anatomical_region': 'anatomy',
    'anatomical region': 'anatomy',
    'site': 'anatomy',
    'body_part': 'anatomy',
    'body part': 'anatomy',
    'region': 'anatomy',
    'laterality': 'anatomy',

    # Scanogram variations
    'scanogram': 'scanogram',
    'scanogram_available': 'scanogram',
    'scanogram available': 'scanogram',
}
SEMANTIC_FIELD_MAP = PATIENT_SEMANTIC_FIELD_MAP


def map_dynamic_field_to_patient_attr(field_name):
    """
    Case-insensitive mapping function linking common dynamic field names to Patient database model attributes.
    Returns: 'cr_number', 'name', 'age', 'gender', or None.
    """
    if not field_name:
        return None
    raw = str(field_name).strip().lower()
    if raw in PATIENT_SEMANTIC_FIELD_MAP:
        return PATIENT_SEMANTIC_FIELD_MAP[raw]

    # Normalize whitespace, underscores, hyphens, and dots
    normalized = re.sub(r'[\s\-_\.]+', '_', raw).strip('_')
    if normalized in PATIENT_SEMANTIC_FIELD_MAP:
        return PATIENT_SEMANTIC_FIELD_MAP[normalized]

    for k, v in PATIENT_SEMANTIC_FIELD_MAP.items():
        if re.sub(r'[\s\-_\.]+', '_', k.strip().lower()).strip('_') == normalized:
            return v
    return None


def get_patient_attribute_for_field(field_name, patient=None, field_type=None):
    """
    Case-insensitive mapping function that resolves common dynamic EDC field names
    (e.g., 'cr_no', 'cr number', 'name', 'patient_name', 'age', 'gender', 'sex')
    to standard Patient database model attributes (Patient.cr_number, Patient.name,
    Patient.age, Patient.gender).

    - If patient is None: returns the mapped attribute name ('cr_number', 'name', 'age', 'gender').
    - If patient is provided: returns the string value from the patient record, or None if empty/unset.
      If field_type is 'Number' and resolving age (e.g. '35 Yrs'), extracts the numeric portion.
    """
    attr_name = map_dynamic_field_to_patient_attr(field_name)
    if not attr_name:
        return None
    if patient is None:
        return attr_name

    val = getattr(patient, attr_name, None)
    # Check alias fallback between name and patient_name
    if val is None and attr_name == 'name':
        val = getattr(patient, 'patient_name', None)
    elif val is None and attr_name == 'patient_name':
        val = getattr(patient, 'name', None)
    elif val is None and attr_name == 'ao_grade':
        val = getattr(patient, 'ao_trauma_score', None)
    elif val is None and attr_name == 'ao_trauma_score':
        val = getattr(patient, 'ao_grade', None)

    if val is None or str(val).strip() == '':
        return None

    val_str = str(val).strip()

    # Extract digits for Number input type or age fields rendered as Number
    if field_type == 'Number' or (field_name and 'age' in str(field_name).lower() and field_type == 'Number'):
        m = re.search(r'\d+', val_str)
        if m:
            return m.group(0)

    return val_str


def is_identifying_patient_field(field_name):
    """
    Checks if a dynamic field is a strictly identifying registry attribute (such as CR Number)
    that should be locked as readonly when pre-filled to prevent desynchronization.
    """
    return map_dynamic_field_to_patient_attr(field_name) == 'cr_number'


MODALITY_DISPLAY_NAMES = {
    'CR': 'X-Ray (CR)',
    'CT': 'CT Scan',
    'MR': 'MRI',
    'DX': 'Digital X-Ray',
}


def format_modality_name(code):
    """Return human-readable clinical modality name to prevent confusion with patient CR Numbers."""
    if not code:
        return ''
    return MODALITY_DISPLAY_NAMES.get(str(code).strip().upper(), str(code))


@app.context_processor
def inject_semantic_edc_utilities():
    return {
        'semantic_field_map': PATIENT_SEMANTIC_FIELD_MAP,
        'patient_semantic_field_map': PATIENT_SEMANTIC_FIELD_MAP,
        'map_dynamic_field_to_patient_attr': map_dynamic_field_to_patient_attr,
        'get_patient_attribute_for_field': get_patient_attribute_for_field,
        'is_identifying_patient_field': is_identifying_patient_field,
        'modality_display_names': MODALITY_DISPLAY_NAMES,
        'format_modality_name': format_modality_name,
    }


def infer_field_type(name, sample_values):
    """Infer field type (Text, Number, Date, File) from column name and sample values."""
    name_lower = name.lower()
    non_empty = [str(v).strip() for v in sample_values if v is not None and str(v).strip()]

    # File check
    file_keywords = ['file', 'image', 'photo', 'attachment', 'pdf', 'scan', 'doc', 'xray', 'radiograph']
    if any(k in name_lower for k in file_keywords):
        return 'File'
    if non_empty and any(any(v.lower().endswith(ext) for ext in ['.dcm', '.png', '.jpg', '.jpeg', '.pdf', '.tiff', '.zip']) for v in non_empty):
        return 'File'

    # Date check
    date_keywords = ['date', 'dob', 'time', 'timestamp', 'visit']
    if any(k in name_lower for k in date_keywords):
        return 'Date'
    if non_empty:
        date_matches = 0
        for v in non_empty:
            for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%Y/%m/%d', '%d-%m-%Y', '%Y%m%d'):
                try:
                    datetime.datetime.strptime(v, fmt)
                    date_matches += 1
                    break
                except ValueError:
                    pass
        if date_matches > 0 and (date_matches / len(non_empty) >= 0.5):
            return 'Date'

    # Number check
    number_keywords = ['score', 'age', 'count', 'size', 'mm', 'cm', 'weight', 'height', 'angle', 'grade', 'kss', 'cobb', 'rate', 'percent', 'dose', 'index', 'hounsfield', 'hu', 'num']
    if any(k in name_lower for k in number_keywords):
        return 'Number'
    if non_empty:
        num_matches = 0
        for v in non_empty:
            try:
                float(v.replace(',', ''))
                num_matches += 1
            except ValueError:
                pass
        if num_matches > 0 and (num_matches / len(non_empty) >= 0.5):
            return 'Number'

    return 'Text'


def infer_schema_from_csv(file_storage):
    """Read first row and sample rows of uploaded CSV to infer EDC column schema."""
    content = file_storage.read()
    if isinstance(content, bytes):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('latin-1')
    else:
        text = str(content)

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r and any(cell.strip() for cell in r)]
    if not rows:
        return []

    headers = [h.strip() for h in rows[0] if h.strip()]
    sample_rows = rows[1:11]

    schema = []
    for idx, header in enumerate(headers):
        samples = [r[idx] for r in sample_rows if idx < len(r)]
        ftype = infer_field_type(header, samples)
        schema.append({
            'name': header,
            'type': ftype
        })
    return schema


@app.route('/trials')
@login_required
def trials_view():
    all_trials = Trial.query.order_by(Trial.created_at.desc()).all()
    selected_trial_id = request.args.get('trial_id')
    active_trial = None
    cohort_patients = []

    if selected_trial_id:
        active_trial = db.session.get(Trial, int(selected_trial_id))
        if active_trial:
            cohort_patients = active_trial.patients.order_by(Patient.created_at.desc()).all()
    elif all_trials:
        active_trial = all_trials[0]
        cohort_patients = active_trial.patients.order_by(Patient.created_at.desc()).all()

    return render_template(
        'trials.html',
        all_trials=all_trials,
        active_trial=active_trial,
        cohort_patients=cohort_patients
    )


@app.route('/trials/create', methods=['POST'])
@login_required
@requires_role('Admin')
def create_trial():
    trial_name = request.form.get('trial_name', '').strip().upper()
    description = request.form.get('description', '').strip()
    target_sample_size_raw = request.form.get('target_sample_size', '').strip()
    required_modalities = request.form.getlist('required_modalities')

    if not trial_name:
        flash("Trial name cannot be empty.", "danger")
        return redirect(url_for('trials_view'))

    existing = Trial.query.filter_by(trial_name=trial_name).first()
    if existing:
        flash(f"Trial '{trial_name}' already exists.", "warning")
        return redirect(url_for('trial_dashboard', trial_id=existing.id))

    target_sample_size = 50
    if target_sample_size_raw:
        try:
            target_sample_size = int(target_sample_size_raw)
        except ValueError:
            target_sample_size = 50

    new_trial = Trial(
        trial_name=trial_name,
        description=description,
        target_sample_size=target_sample_size
    )
    new_trial.set_required_modalities(required_modalities)

    # Check for optional initial template file
    template_file = request.files.get('template_file')
    if template_file and template_file.filename:
        inferred = infer_schema_from_csv(template_file)
        if inferred:
            new_trial.set_data_schema(inferred)

    db.session.add(new_trial)
    db.session.commit()
    flash(f"Clinical Trial '{trial_name}' created successfully with target sample size {target_sample_size}.", "success")
    return redirect(url_for('trial_dashboard', trial_id=new_trial.id))


@app.route('/trials/<int:trial_id>/edit', methods=['POST'])
@login_required
@requires_role('Admin')
def edit_trial(trial_id):
    """Update parameters for an existing clinical trial protocol."""
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    trial_name = request.form.get('trial_name', '').strip().upper()
    description = request.form.get('description', '').strip()
    target_sample_size_raw = request.form.get('target_sample_size', '').strip()
    required_modalities = request.form.getlist('required_modalities')

    if not trial_name:
        flash("Trial name cannot be empty.", "danger")
        return redirect(url_for('trial_dashboard', trial_id=trial_id))

    if trial_name != trial.trial_name:
        existing = Trial.query.filter_by(trial_name=trial_name).first()
        if existing:
            flash(f"Another trial with name '{trial_name}' already exists.", "danger")
            return redirect(url_for('trial_dashboard', trial_id=trial_id))
        trial.trial_name = trial_name

    trial.description = description
    if target_sample_size_raw:
        try:
            trial.target_sample_size = int(target_sample_size_raw)
        except ValueError:
            pass

    trial.set_required_modalities(required_modalities)
    db.session.commit()

    flash(f"Trial '{trial.trial_name}' parameters successfully updated.", "success")
    return redirect(url_for('trial_dashboard', trial_id=trial.id))


@app.route('/trials/<int:trial_id>/delete', methods=['POST'])
@login_required
@requires_role('Admin')
def delete_trial(trial_id):
    """Drop a trial from the database.
    Strict safety rule: Unlinks trial from patients and radiology records without deleting
    the underlying patient or scan entities.
    """
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    trial_name = trial.trial_name
    # 1. Unlink all enrolled patients from association table
    db.session.execute(patient_trials.delete().where(patient_trials.c.trial_id == trial.id))
    # 2. Unlink all associated scans from association table
    db.session.execute(scan_trials.delete().where(scan_trials.c.trial_id == trial.id))
    # 3. Delete trial-specific dynamic EDC records
    TrialPatientData.query.filter_by(trial_id=trial.id).delete()
    # 4. Remove trial entity
    db.session.delete(trial)
    db.session.commit()

    flash(f"Trial '{trial_name}' was successfully deleted. Enrolled patients and radiology records remain preserved in the registry.", "info")
    return redirect(url_for('trials_view'))


@app.route('/trials/<int:trial_id>/import-template', methods=['POST'])
@login_required
@requires_role('Admin')
def import_trial_template(trial_id):
    """Import CSV data template to dynamically extract headers and infer data types."""
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    file = request.files.get('template_file')
    if not file or not file.filename:
        flash("Please select a CSV template file to upload.", "danger")
        return redirect(url_for('trial_dashboard', trial_id=trial_id))

    try:
        schema = infer_schema_from_csv(file)
        if not schema:
            flash("The uploaded CSV did not contain any valid headers in the first row.", "warning")
            return redirect(url_for('trial_dashboard', trial_id=trial_id))

        trial.set_data_schema(schema)
        db.session.commit()
        field_names = ", ".join([f"{f['name']} ({f['type']})" for f in schema])
        flash(f"Successfully imported EDC template with {len(schema)} clinical parameters: {field_names}", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error importing CSV template: {str(e)}", "danger")

    return redirect(url_for('trial_dashboard', trial_id=trial_id))


@app.route('/trials/<int:trial_id>/schema', methods=['POST'])
@app.route('/trials/<int:trial_id>/edit-schema', methods=['POST'])
@login_required
@requires_role('Admin', 'Editor')
def update_trial_schema(trial_id):
    """Update Trial EDC Schema (Rename variables, change data types, add/delete parameters)
    and safely migrate stored TrialPatientData records.
    """
    trial = db.session.get(Trial, trial_id)
    if not trial:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': "Trial not found."}), 404
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    json_payload = request.get_json(silent=True) if request.is_json else None

    new_schema = []
    renamed_map = {}  # old_name -> new_name
    deleted_names = set()

    if json_payload and isinstance(json_payload.get('fields'), list):
        field_list = json_payload.get('fields')
        for item in field_list:
            name = str(item.get('name', '')).strip()
            raw_type = str(item.get('type', 'Text')).strip()
            type_val = 'File' if 'File' in raw_type or 'Image' in raw_type or 'PDF' in raw_type else raw_type
            if type_val not in ('Text', 'Number', 'Date', 'File'):
                type_val = 'Text'
            old_name = str(item.get('old_name', '')).strip() or name
            if name:
                new_schema.append({'name': name, 'type': type_val})
                if old_name and old_name != name:
                    renamed_map[old_name] = name
        deleted_names = set(json_payload.get('deleted', []))
    else:
        # Form submission: field_name[], field_type[], old_name[]
        names = request.form.getlist('field_name[]') or request.form.getlist('field_name')
        types = request.form.getlist('field_type[]') or request.form.getlist('field_type')
        old_names = request.form.getlist('old_name[]') or request.form.getlist('old_name')
        deleted_list = request.form.getlist('deleted_fields[]') or request.form.getlist('deleted_fields')
        deleted_names = set(d.strip() for d in deleted_list if d.strip())

        if not names:
            idx = 0
            while f"name_{idx}" in request.form or f"field_{idx}_name" in request.form:
                n = (request.form.get(f"name_{idx}") or request.form.get(f"field_{idx}_name") or '').strip()
                t = (request.form.get(f"type_{idx}") or request.form.get(f"field_{idx}_type") or 'Text').strip()
                o = (request.form.get(f"old_name_{idx}") or request.form.get(f"field_{idx}_old_name") or n).strip()
                if n:
                    names.append(n)
                    types.append(t)
                    old_names.append(o)
                idx += 1

        for i, n in enumerate(names):
            n_clean = n.strip()
            if not n_clean:
                continue
            raw_t = types[i].strip() if i < len(types) else 'Text'
            t_clean = 'File' if 'File' in raw_t or 'Image' in raw_t or 'PDF' in raw_t else raw_t
            if t_clean not in ('Text', 'Number', 'Date', 'File'):
                t_clean = 'Text'
            o_clean = old_names[i].strip() if i < len(old_names) else n_clean
            new_schema.append({'name': n_clean, 'type': t_clean})
            if o_clean and o_clean != n_clean:
                renamed_map[o_clean] = n_clean

    # Detect implicitly deleted fields
    old_schema = trial.get_data_schema()
    new_names_set = {f['name'] for f in new_schema}
    for old_field in old_schema:
        of_name = old_field.get('name')
        if of_name and of_name not in new_names_set and of_name not in renamed_map:
            deleted_names.add(of_name)

    # Safely migrate all TrialPatientData records
    tpd_records = TrialPatientData.query.filter_by(trial_id=trial.id).all()
    for tpd in tpd_records:
        data = tpd.get_data()
        changed = False
        for old_k, new_k in renamed_map.items():
            if old_k in data:
                data[new_k] = data.pop(old_k)
                changed = True
        for del_k in deleted_names:
            if del_k in data:
                data.pop(del_k, None)
                changed = True
        if changed:
            tpd.set_data(data)

    trial.set_data_schema(new_schema)
    db.session.commit()

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'message': f"EDC Schema for '{trial.trial_name}' updated successfully ({len(new_schema)} variables).",
            'schema': new_schema
        })

    flash(f"EDC Schema for '{trial.trial_name}' updated successfully ({len(new_schema)} variables).", "success")
    return redirect(url_for('trial_dashboard', trial_id=trial_id))


@app.route('/trials/<int:trial_id>')
@app.route('/trials/<int:trial_id>/dashboard')
@login_required
def trial_dashboard(trial_id):
    """Clinical Trial Dashboard displaying analytics, recruited patient sidebar, and dynamic EDC forms."""
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    patients = trial.patients.order_by(Patient.cr_number).all()
    target_sample_size = trial.target_sample_size or 50
    recruited_count = len(patients)
    recruitment_percentage = round(min(100.0, (recruited_count / target_sample_size * 100)), 1) if target_sample_size > 0 else 100.0
    remaining_needed = max(0, target_sample_size - recruited_count)

    required_modalities = trial.get_required_modalities()
    schema = trial.get_data_schema()
    schema_fields = [f['name'] for f in schema if isinstance(f, dict) and 'name' in f]

    # Pre-fetch all trial patient data records
    tpd_records = TrialPatientData.query.filter_by(trial_id=trial.id).all()
    tpd_map = {tpd.cr_number: tpd for tpd in tpd_records}

    total_criteria_possible = 0
    total_criteria_fulfilled = 0
    patient_stats = []

    # Modality fulfillment count across whole trial
    modality_stats = {m: {'name': m, 'display_name': format_modality_name(m), 'count': 0} for m in required_modalities}

    for p in patients:
        p_modalities = {s.modality.upper() for s in p.scans if s.modality}

        # Check required modalities
        mods_fulfilled = 0
        for m in required_modalities:
            if m.upper() in p_modalities:
                mods_fulfilled += 1
                modality_stats[m]['count'] += 1

        # Check schema fields
        tpd = tpd_map.get(p.cr_number)
        p_data = tpd.get_data() if tpd else {}
        fields_fulfilled = 0
        for f_name in schema_fields:
            val = p_data.get(f_name)
            if val is None or str(val).strip() == '':
                val = get_patient_attribute_for_field(f_name, p)
            if val is not None and str(val).strip() != '':
                fields_fulfilled += 1

        p_total_req = len(required_modalities) + len(schema_fields)
        p_total_done = mods_fulfilled + fields_fulfilled
        p_pct = round((p_total_done / p_total_req * 100), 1) if p_total_req > 0 else 100.0

        total_criteria_possible += p_total_req
        total_criteria_fulfilled += p_total_done

        patient_stats.append({
            'patient': p,
            'data': p_data,
            'modalities_present': p_modalities,
            'mods_fulfilled': mods_fulfilled,
            'fields_fulfilled': fields_fulfilled,
            'total_req': p_total_req,
            'total_done': p_total_done,
            'completeness_pct': p_pct,
            'missing_modalities': [m for m in required_modalities if m.upper() not in p_modalities]
        })

    if total_criteria_possible > 0:
        overall_completeness = round((total_criteria_fulfilled / total_criteria_possible * 100), 1)
    else:
        overall_completeness = 100.0 if recruited_count > 0 else 0.0

    # Determine active patient for dynamic form
    selected_cr = request.args.get('cr_number')
    active_patient_stat = None
    if selected_cr:
        for ps in patient_stats:
            if ps['patient'].cr_number == selected_cr:
                active_patient_stat = ps
                break
    if not active_patient_stat and patient_stats:
        active_patient_stat = patient_stats[0]

    # Find patients in registry not yet enrolled
    enrolled_crs = {p.cr_number for p in patients}
    available_patients = Patient.query.filter(~Patient.cr_number.in_(enrolled_crs)).order_by(Patient.cr_number).all() if enrolled_crs else Patient.query.order_by(Patient.cr_number).all()

    # Clinical cohort context aggregations
    cohort_diagnoses = [p.diagnosis for p in patients if p.diagnosis]
    cohort_ao_scores = [p.ao_trauma_score for p in patients if p.ao_trauma_score]
    cohort_anatomies = [p.anatomy for p in patients if p.anatomy]

    return render_template(
        'trial_dashboard.html',
        trial=trial,
        patients=patients,
        patient_stats=patient_stats,
        active_patient_stat=active_patient_stat,
        target_sample_size=target_sample_size,
        recruited_count=recruited_count,
        recruitment_percentage=recruitment_percentage,
        remaining_needed=remaining_needed,
        overall_completeness=overall_completeness,
        total_criteria_fulfilled=total_criteria_fulfilled,
        total_criteria_possible=total_criteria_possible,
        required_modalities=required_modalities,
        modality_stats=modality_stats,
        modality_display_names=MODALITY_DISPLAY_NAMES,
        format_modality_name=format_modality_name,
        schema=schema,
        dynamic_schema=schema,
        available_patients=available_patients,
        cohort_diagnoses=cohort_diagnoses,
        cohort_ao_scores=cohort_ao_scores,
        cohort_anatomies=cohort_anatomies,
        semantic_field_map=PATIENT_SEMANTIC_FIELD_MAP,
        patient_semantic_field_map=PATIENT_SEMANTIC_FIELD_MAP,
        get_patient_attribute_for_field=get_patient_attribute_for_field,
        is_identifying_patient_field=is_identifying_patient_field
    )


@app.route('/trials/<int:trial_id>/patient/<cr_number>/edc', methods=['POST'])
@login_required
@requires_role('Editor')
def save_trial_patient_edc(trial_id, cr_number):
    """Save dynamically generated EDC form parameters for a patient in this trial."""
    trial = db.session.get(Trial, trial_id)
    patient = db.session.get(Patient, cr_number)
    if not trial or not patient:
        flash("Trial or Patient not found.", "danger")
        return redirect(url_for('trials_view'))

    tpd = TrialPatientData.query.filter_by(trial_id=trial.id, cr_number=patient.cr_number).first()
    if not tpd:
        tpd = TrialPatientData(trial_id=trial.id, cr_number=patient.cr_number)
        db.session.add(tpd)

    data_dict = tpd.get_data()
    schema = trial.get_data_schema()

    for idx, field in enumerate(schema):
        f_name = field.get('name')
        f_type = field.get('type', 'Text')
        if not f_name:
            continue

        if f_type in ('File', 'Image'):
            uploaded_file = request.files.get(f'file_{idx}')
            if uploaded_file and uploaded_file.filename:
                fname = secure_filename(uploaded_file.filename)
                unique_fname = f"edc_{trial.id}_{cr_number}_{int(datetime.datetime.utcnow().timestamp())}_{fname}"
                dest_path = os.path.join(app.config['UPLOAD_FOLDER'], 'custom_files', unique_fname)
                uploaded_file.save(dest_path)
                data_dict[f_name] = f"/uploads/custom_files/{unique_fname}"
            else:
                existing_file = request.form.get(f'existing_file_{idx}', '').strip()
                if existing_file:
                    data_dict[f_name] = existing_file
        else:
            val = request.form.get(f'field_{idx}', '').strip()
            if not val:
                prefill = get_patient_attribute_for_field(f_name, patient, f_type)
                if prefill:
                    val = prefill
            data_dict[f_name] = val

    tpd.set_data(data_dict)
    tpd.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.session.commit()

    flash(f"Clinical EDC parameters successfully saved for Patient {patient.cr_number}.", "success")
    return redirect(url_for('trial_dashboard', trial_id=trial.id, cr_number=patient.cr_number))


@app.route('/trials/<int:trial_id>/enroll', methods=['POST'])
@login_required
@requires_role('Editor')
def enroll_trial_patient(trial_id):
    """Enroll a registry patient into this trial."""
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    cr_number = request.form.get('cr_number', '').strip()
    patient = db.session.get(Patient, cr_number)
    if not patient:
        flash(f"Patient with CR Number '{cr_number}' not found in registry.", "danger")
        return redirect(url_for('trial_dashboard', trial_id=trial_id))

    if patient not in trial.patients:
        trial.patients.append(patient)
        for scan in patient.scans:
            if trial not in scan.trials:
                scan.trials.append(trial)
        db.session.commit()
        flash(f"Patient {patient.cr_number} ({patient.patient_name}) enrolled into {trial.trial_name}.", "success")
    else:
        flash(f"Patient {patient.cr_number} is already enrolled in {trial.trial_name}.", "info")

    return redirect(url_for('trial_dashboard', trial_id=trial_id, cr_number=patient.cr_number))


@app.route('/trials/<int:trial_id>/remove-patient/<cr_number>', methods=['POST'])
@login_required
@requires_role('Admin')
def remove_trial_patient(trial_id, cr_number):
    """Remove a patient and their EDC records from this trial."""
    trial = db.session.get(Trial, trial_id)
    patient = db.session.get(Patient, cr_number)
    if trial and patient and patient in trial.patients:
        trial.patients.remove(patient)
        TrialPatientData.query.filter_by(trial_id=trial.id, cr_number=patient.cr_number).delete()
        db.session.commit()
        flash(f"Patient {cr_number} removed from trial {trial.trial_name}.", "info")

    return redirect(url_for('trial_dashboard', trial_id=trial_id))


@app.route('/trials/<int:trial_id>/export')
@login_required
def export_trial_cohort(trial_id):
    """Export trial cohort including dynamic EDC parameters using io.StringIO and standard csv module."""
    trial = db.session.get(Trial, trial_id)
    if not trial:
        flash("Trial not found.", "danger")
        return redirect(url_for('trials_view'))

    schema = trial.get_data_schema()
    schema_fields = [f['name'] for f in schema if isinstance(f, dict) and 'name' in f]

    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        'CR Number', 'Patient Name', 'Age', 'Gender',
        'Diagnosis', 'AO Grade', 'AO Trauma Score', 'Anatomy', 'Scanogram',
        'Registered Date', 'Total Scans', 'Scans Summary'
    ] + schema_fields
    writer.writerow(headers)

    tpd_records = TrialPatientData.query.filter_by(trial_id=trial.id).all()
    tpd_map = {tpd.cr_number: tpd.get_data() for tpd in tpd_records}

    for p in trial.patients:
        scan_list = [f"{s.modality} ({s.date_of_test or 'N/A'})" for s in p.scans]
        p_edc = tpd_map.get(p.cr_number, {})
        reg_date = p.created_at.strftime('%Y-%m-%d %H:%M') if p.created_at else ''

        row = [
            p.cr_number,
            p.patient_name,
            p.age or '',
            p.gender or '',
            p.diagnosis or '',
            p.ao_grade or p.ao_trauma_score or '',
            p.ao_trauma_score or p.ao_grade or '',
            p.anatomy or '',
            p.scanogram or 'No',
            reg_date,
            len(p.scans),
            "; ".join(scan_list)
        ]
        for f in schema_fields:
            val = p_edc.get(f, '')
            if not val:
                val = get_patient_attribute_for_field(f, p) or ''
            row.append(val)
        writer.writerow(row)

    csv_data = output.getvalue()
    filename = f"{trial.trial_name}_Cohort.csv"
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename={filename}'
        }
    )


# ---------------------------------------------------------
# DICOM VIEWER & STREAMING ENGINE
# ---------------------------------------------------------

@app.route('/viewer/<int:scan_id>')
@login_required
def dicom_viewer(scan_id):
    scan = db.session.get(Scan, scan_id)
    if not scan:
        flash("Requested scan not found in registry.", "danger")
        return redirect(url_for('dashboard'))

    # Retrieve all series of this patient for drawer navigation
    patient_scans = Scan.query.filter_by(cr_number=scan.cr_number).order_by(Scan.id).all()
    instance_files = scan.get_instance_files()
    has_cloud_id = bool(scan.drive_file_id and not str(scan.drive_file_id).startswith('local_'))
    return render_template(
        'viewer.html',
        scan=scan,
        patient=scan.patient,
        patient_scans=patient_scans,
        instance_files=instance_files,
        slice_count=len(instance_files),
        drive_file_id=scan.drive_file_id,
        is_drive_configured=drive_service.is_configured(),
        has_cloud_id=has_cloud_id
    )


@app.route('/api/drive/token')
@login_required
def api_drive_token():
    """Returns Google Drive OAuth access token for client-side direct Google Drive API streaming."""
    token = drive_service.get_access_token() if drive_service.is_configured() else None
    return jsonify({
        'configured': drive_service.is_configured(),
        'access_token': token
    })


@app.route('/api/patient/<cr_number>/series')
@login_required
def api_patient_series(cr_number):
    """Return JSON list of all grouped radiology series for a given patient."""
    patient = db.session.get(Patient, cr_number)
    if not patient:
        return jsonify({'error': 'Patient not found'}), 404

    series_data = []
    for s in patient.scans:
        series_data.append({
            'id': s.id,
            'modality': s.modality,
            'date_of_test': s.date_of_test or 'N/A',
            'series_description': s.series_description or 'Orthopedic Evaluation',
            'instance_count': s.instance_count or 1,
            'series_instance_uid': s.series_instance_uid or '',
            'viewer_url': url_for('dicom_viewer', scan_id=s.id)
        })
    return jsonify({
        'cr_number': patient.cr_number,
        'patient_name': patient.patient_name,
        'age': patient.age or 'Unknown',
        'gender': patient.gender or 'Other',
        'series': series_data
    })


def resolve_dicom_file(path_str):
    """Resolve a DICOM path on local filesystem with fallbacks for common folder typos and relative paths."""
    if not path_str or not isinstance(path_str, str):
        return None
    # 1. Exact path
    if os.path.isfile(path_str):
        return path_str
    # 2. Known common folder typo recovery (e.g. Never Delete0 vs Never Delete))
    if 'Never Delete0' in path_str:
        alt = path_str.replace('Never Delete0', 'Never Delete)')
        if os.path.isfile(alt):
            return alt
    if 'Never Delete)' in path_str:
        alt = path_str.replace('Never Delete)', 'Never Delete0')
        if os.path.isfile(alt):
            return alt
    # 3. Relative to application root directory
    rel_p = os.path.join(app.root_path, path_str)
    if os.path.isfile(rel_p):
        return rel_p
    # 4. Fallback in sample_dicoms
    base_p = os.path.basename(path_str)
    sample_p = os.path.join(app.root_path, 'sample_dicoms', base_p)
    if os.path.isfile(sample_p):
        return sample_p
    # 5. Fallback in uploads/dicom
    upload_p = os.path.join(app.root_path, 'uploads', 'dicom', base_p)
    if os.path.isfile(upload_p):
        return upload_p
    return None


@app.route('/scan/<int:scan_id>/dicom')
@app.route('/api/dicom/<int:scan_id>')
@login_required
def stream_dicom(scan_id):
    """Stream raw DICOM bytes directly from local storage or Drive to the Cornerstone.js viewer."""
    try:
        scan = db.session.get(Scan, scan_id)
        if not scan:
            err_msg = f"[DICOM Streaming Error] Scan #{scan_id} not found in database."
            print(err_msg)
            return Response(f"Scan #{scan_id} not found", status=404, mimetype='text/plain')

        # 1. Build and search candidate local file paths
        candidate_paths = []
        if scan.local_file_path:
            candidate_paths.append(scan.local_file_path)
        if scan.local_storage_path and scan.local_storage_path not in candidate_paths:
            candidate_paths.append(scan.local_storage_path)

        for f in scan.get_instance_files():
            if f and f not in candidate_paths:
                candidate_paths.append(f)

        resolved_path = None
        for p in candidate_paths:
            resolved_path = resolve_dicom_file(p)
            if resolved_path:
                break

        if resolved_path and os.path.exists(resolved_path):
            print(f"[DICOM Streaming] Streaming Scan #{scan_id} ({scan.modality}) directly from local path: {resolved_path}")
            return send_file(
                resolved_path,
                mimetype='application/dicom',
                as_attachment=False,
                download_name=scan.file_name or os.path.basename(resolved_path) or "image.dcm"
            )

        # 2. Remote Google Drive API fallback if file is not locally stored
        if scan.drive_file_id and not scan.drive_file_id.startswith('local_'):
            try:
                print(f"[On-Demand PACS Viewer] Streaming raw DICOM for Scan #{scan.id} from Drive API...")
                dicom_bytes = drive_service.download_file_to_memory(scan.drive_file_id)
                return Response(
                    dicom_bytes,
                    mimetype='application/dicom',
                    headers={
                        'Content-Type': 'application/dicom',
                        'Content-Disposition': f'inline; filename="{scan.file_name or "image.dcm"}"',
                        'Content-Length': len(dicom_bytes),
                        'Cache-Control': 'public, max-age=3600'
                    }
                )
            except Exception as e:
                err_detail = traceback.format_exc()
                print(f"[DICOM Streaming Error] Failed to stream from Google Drive for Scan #{scan_id}:\n{err_detail}")
                return Response(f"Error streaming DICOM from Drive: {str(e)}", status=502, mimetype='text/plain')

        # If not found locally or in Drive
        raise FileNotFoundError(f"DICOM file not found on local disk for Scan #{scan_id}. Searched candidate paths: {candidate_paths}")

    except Exception as e:
        err_detail = traceback.format_exc()
        print(f"[DICOM Streaming Fatal Error] Scan #{scan_id} failed to stream:\n{err_detail}")
        status_code = 404 if isinstance(e, FileNotFoundError) else 500
        return Response(f"DICOM Stream Error: {str(e)}", status=status_code, mimetype='text/plain')


@app.route('/scan/<int:scan_id>/slice/<int:slice_idx>')
@app.route('/api/dicom/<int:scan_id>/slice/<int:slice_idx>')
@login_required
def stream_dicom_slice(scan_id, slice_idx):
    """Stream a specific slice of a multi-slice DICOM series."""
    try:
        scan = db.session.get(Scan, scan_id)
        if not scan:
            err_msg = f"[DICOM Slice Error] Scan #{scan_id} not found."
            print(err_msg)
            return Response(f"Scan #{scan_id} not found", status=404, mimetype='text/plain')

        files = scan.get_instance_files()
        if 0 <= slice_idx < len(files):
            slice_path = files[slice_idx]
            resolved = resolve_dicom_file(slice_path)
            if resolved and os.path.exists(resolved):
                return send_file(
                    resolved,
                    mimetype='application/dicom',
                    as_attachment=False,
                    download_name=os.path.basename(resolved)
                )

        # Fallback to primary scan stream
        return stream_dicom(scan_id)
    except Exception as e:
        err_detail = traceback.format_exc()
        print(f"[DICOM Slice Streaming Error] Scan #{scan_id}, slice {slice_idx}:\n{err_detail}")
        return Response(f"DICOM Slice Error: {str(e)}", status=500, mimetype='text/plain')


# ---------------------------------------------------------
# BACKGROUND METADATA CRAWLER ENGINE & DIRECTORY SYNC
# ---------------------------------------------------------

def start_background_crawler(app_instance, target_path=None):
    r"""Launch asynchronous background metadata crawler across local filesystem (D:\RADIOLOGY DATA) or Drive."""
    global crawler_state
    target_path = target_path or LOCAL_DRIVE_PATH

    if crawler_state['is_running']:
        print(f"[Crawler] A sync is already in progress for '{crawler_state['current_folder']}'.")
        return False

    def worker():
        global crawler_state
        crawler_state['is_running'] = True
        crawler_state['current_folder'] = target_path
        crawler_state['folders_scanned'] = 0
        crawler_state['files_scanned'] = 0
        crawler_state['ingested'] = 0
        crawler_state['skipped'] = 0
        crawler_state['total_files'] = 0
        crawler_state['current_file_idx'] = 0
        crawler_state['last_patient'] = None
        crawler_state['last_error'] = None
        crawler_state['started_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        try:
            with app_instance.app_context():
                def on_progress(p):
                    if 'folders_scanned' in p:
                        crawler_state['folders_scanned'] = p['folders_scanned']
                    if 'files_scanned' in p:
                        crawler_state['files_scanned'] = p['files_scanned']
                        crawler_state['current_file_idx'] = p['files_scanned']
                    elif 'current' in p:
                        crawler_state['current_file_idx'] = p['current']
                        crawler_state['files_scanned'] = p['current']
                    if 'total' in p:
                        crawler_state['total_files'] = p['total']
                    crawler_state['ingested'] = p.get('ingested', crawler_state['ingested'])
                    crawler_state['skipped'] = p.get('skipped', crawler_state['skipped'])
                    if 'last_patient' in p:
                        crawler_state['last_patient'] = p['last_patient']

                models = {'Patient': Patient, 'Scan': Scan}

                # Priority: If target_path is a local directory, run lightning-fast os.walk scanner
                if os.path.exists(target_path) and os.path.isdir(target_path):
                    print(f"[Local Crawler] Scanning local directory tree: {target_path}")
                    res = scan_local_directory(target_path, db.session, models, on_progress=on_progress, drive_service=drive_service)
                    crawler_state['folders_scanned'] = res.get('folders_scanned', res.get('scanned_dirs', crawler_state['folders_scanned']))
                    crawler_state['files_scanned'] = res.get('files_scanned', crawler_state['files_scanned'])
                    crawler_state['total_files'] = res.get('files_scanned', crawler_state['files_scanned'])
                    crawler_state['current_file_idx'] = crawler_state['files_scanned']
                    crawler_state['ingested'] = res.get('ingested', crawler_state['ingested'])
                    crawler_state['skipped'] = res.get('skipped', crawler_state['skipped'])
                else:
                    # Otherwise handle as Google Drive Folder ID
                    if not drive_service.is_configured():
                        print(f"[Drive Crawler] Google Drive credentials not yet configured. Skipping crawler for folder '{target_path}'.")
                        crawler_state['is_running'] = False
                        return
                    print(f"[Drive Crawler] Scanning remote Google Drive folder: {target_path}")
                    res = drive_service.sync_folder(target_path, db.session, models, recursive=True, on_progress=on_progress)
                    crawler_state['folders_scanned'] = res.get('folders_scanned', 1)
                    crawler_state['files_scanned'] = res.get('total_found', 0)
                    crawler_state['total_files'] = res.get('total_found', 0)
                    crawler_state['current_file_idx'] = res.get('total_found', 0)
                    crawler_state['ingested'] = res.get('ingested', 0)
                    crawler_state['skipped'] = res.get('skipped', 0)
        except Exception as e:
            crawler_state['last_error'] = str(e)
            print(f"[Crawler] Error during crawl: {e}")
        finally:
            crawler_state['is_running'] = False
            print(f"[Crawler] Background crawl worker thread finished.")

    crawler_thread = threading.Thread(target=worker, daemon=True, name="BackgroundRadiologyCrawler")
    crawler_thread.start()
    return True


@app.route('/api/crawler-status')
@login_required
def api_crawler_status():
    """Live JSON endpoint for real-time dashboard polling of crawler telemetry."""
    total_patients = Patient.query.count()
    total_scans = Scan.query.count()
    return jsonify({
        'crawler': crawler_state,
        'folders_scanned': crawler_state.get('folders_scanned', 0),
        'files_scanned': crawler_state.get('files_scanned', crawler_state.get('current_file_idx', 0)),
        'ingested': crawler_state.get('ingested', 0),
        'skipped': crawler_state.get('skipped', 0),
        'total_patients': total_patients,
        'total_scans': total_scans
    })


@app.route('/drive-sync', methods=['GET', 'POST'])
@app.route('/sync-directory', methods=['GET', 'POST'])
@login_required
@requires_role('Admin')
def drive_sync():
    active_target = LOCAL_DRIVE_PATH

    if request.method == 'POST':
        target_path = request.form.get('target_path', '').strip() or request.form.get('folder_id', '').strip() or LOCAL_DRIVE_PATH
        active_target = target_path

        if crawler_state['is_running']:
            flash(f"A background scan is already actively running for '{crawler_state['current_folder']}'.", "warning")
        else:
            started = start_background_crawler(app, target_path)
            if started:
                flash(f"Asynchronous scan initiated for '{target_path}'. Patients will populate the dashboard in real-time.", "info")
            else:
                flash("Could not initiate scan.", "danger")

        return redirect(url_for('dashboard'))

    return render_template(
        'drive_sync.html',
        is_local_available=os.path.exists(LOCAL_DRIVE_PATH),
        local_drive_path=LOCAL_DRIVE_PATH,
        default_folder_id=LOCAL_DRIVE_PATH,
        default_drive_folder_id=DEFAULT_DRIVE_FOLDER_ID,
        is_configured=drive_service.is_configured(),
        crawler=crawler_state,
        active_target=active_target
    )


@app.route('/api/upload-dicom', methods=['POST'])
@login_required
@requires_role('Editor')
def upload_dicom_file():
    """Endpoint allowing direct upload of DICOM files for clinical ingestion & testing with smart auto-mapping."""
    file = request.files.get('dicom_file')
    if not file or not file.filename:
        flash("No file was selected for upload.", "danger")
        return redirect(url_for('dashboard'))

    fname = secure_filename(file.filename)
    dest_path = os.path.join(app.config['UPLOAD_FOLDER'], 'dicom', f"{int(datetime.datetime.utcnow().timestamp())}_{fname}")
    file.save(dest_path)

    try:
        with open(dest_path, 'rb') as f:
            dicom_bytes = f.read()

        cloud_file_id = None
        if drive_service.is_configured():
            try:
                cloud_file_id = drive_service.find_file_id_by_filename(fname)
            except Exception:
                pass

        meta = parse_dicom_bytes(dicom_bytes, fallback_name=fname, drive_file_id=cloud_file_id or f"local_{fname}", filepath=dest_path)

        # Smart DICOM Auto-Mapping against existing pre-scan registered patients
        patient, is_new = find_matching_patient(db.session, Patient, meta)
        if not patient:
            patient = Patient(
                cr_number=meta['cr_number'],
                patient_name=meta['patient_name'] or 'Unknown Patient',
                age=meta['age'] or 'Unknown',
                gender=meta['gender'] or 'Other'
            )
            db.session.add(patient)
            db.session.flush()
        else:
            # If mapped to existing manual pre-scan entry, update any missing clinical fields
            if (not patient.patient_name or patient.patient_name in ('Anonymous', 'Unknown Patient', 'Unknown', 'Pre-Scan Registered Patient')) and meta['patient_name'] not in ('Anonymous', 'Unknown Patient', 'Unknown'):
                patient.patient_name = meta['patient_name']
            if (not patient.age or patient.age in ('Unknown', 'N/A')) and meta['age'] not in ('Unknown', 'N/A'):
                patient.age = meta['age']
            if (not patient.gender or patient.gender in ('Other', 'Unknown')) and meta['gender'] not in ('Other', 'Unknown'):
                patient.gender = meta['gender']

        # Grouping by SeriesInstanceUID
        series_uid = meta.get('series_instance_uid')
        existing_series = None
        if series_uid:
            existing_series = Scan.query.filter_by(
                cr_number=patient.cr_number,
                series_instance_uid=series_uid
            ).first()
        if not existing_series and not series_uid:
            existing_series = Scan.query.filter_by(
                cr_number=patient.cr_number,
                modality=meta['modality'],
                date_of_test=meta['date_of_test'],
                series_description=meta['series_description']
            ).first()

        if existing_series:
            existing_series.add_instance_file(dest_path)
            if cloud_file_id and not existing_series.drive_file_id:
                existing_series.drive_file_id = cloud_file_id
            db.session.commit()
            flash(f"Added slice to existing Series #{existing_series.id} ({meta['modality']} - {existing_series.instance_count} slices) for Patient {patient.cr_number}.", "success")
        else:
            import json
            scan = Scan(
                cr_number=patient.cr_number,
                drive_file_id=cloud_file_id or f"local_{fname}",
                file_name=fname,
                modality=meta['modality'],
                date_of_test=meta['date_of_test'],
                series_description=meta['series_description'],
                study_instance_uid=meta['study_instance_uid'],
                series_instance_uid=series_uid or f"SERIES_{int(datetime.datetime.now().timestamp())}_{fname}",
                sop_instance_uid=meta['sop_instance_uid'],
                local_file_path=dest_path,
                file_size_bytes=len(dicom_bytes),
                instance_count=1,
                instance_files=json.dumps([dest_path])
            )
            db.session.add(scan)
            db.session.commit()
            if not is_new:
                flash(f"Auto-mapped DICOM Series to existing Pre-Scan Patient record: {patient.cr_number} ({patient.patient_name}).", "success")
            else:
                flash(f"New DICOM Series ingested successfully for Patient {patient.cr_number} ({patient.patient_name}).", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error parsing uploaded DICOM: {str(e)}", "danger")

    return redirect(url_for('dashboard'))


@app.route('/export')
@app.route('/export/patients')
@login_required
def export_all_patients():
    """Export complete patient registry to CSV using standard csv module and io.StringIO buffer."""
    patients = Patient.query.order_by(Patient.cr_number).all()
    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        'CR Number', 'Patient Name', 'Age', 'Gender',
        'Diagnosis', 'AO Grade', 'AO Trauma Score', 'Anatomy', 'Scanogram',
        'Registration Date', 'Total Scans', 'Assigned Trials', 'Custom Data'
    ]
    writer.writerow(headers)

    for p in patients:
        trials_str = ", ".join([t.trial_name for t in p.trials])
        custom_str = "; ".join([f"{cd.field_name}: {cd.field_value}" for cd in p.custom_data])
        reg_date = p.created_at.strftime('%Y-%m-%d') if p.created_at else ''
        writer.writerow([
            p.cr_number,
            p.patient_name,
            p.age or '',
            p.gender or '',
            p.diagnosis or '',
            p.ao_grade or p.ao_trauma_score or '',
            p.ao_trauma_score or p.ao_grade or '',
            p.anatomy or '',
            p.scanogram or 'No',
            reg_date,
            len(p.scans),
            trials_str,
            custom_str
        ])

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=cohort.csv'
        }
    )


# ---------------------------------------------------------
# STARTUP MIGRATIONS & BACKGROUND AUTO-SYNC HOOK
# ---------------------------------------------------------

def run_retroactive_cr_migration(engine_or_conn=None):
    """Retroactive Startup Migration:
    Parses all existing Patient records in SQLite, extracts the strict 12-digit CR number
    starting with 19 or 20 (regex: \\b(?:19|20)\\d{10}\\b) into the cr_number column,
    cleans the patient_name column, and safely cascades all foreign key references
    (scans, custom_data, patient_trials, trial_patient_data) while merging duplicate records.
    """
    try:
        def do_migration(conn):
            tables = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if 'patients' not in tables:
                return

            rows = conn.exec_driver_sql("SELECT cr_number, patient_name FROM patients").fetchall()
            migrated_count = 0
            for old_cr, old_name in rows:
                if not old_cr:
                    continue
                combined = f"{old_cr} {old_name or ''}"
                m = re.search(r'\b((?:19|20)\d{10})\b', combined)
                new_name = clean_patient_name(old_name)

                if not m:
                    # Clean name if needed
                    if new_name != old_name and new_name:
                        conn.exec_driver_sql("UPDATE patients SET patient_name = ? WHERE cr_number = ?", (new_name, old_cr))
                    continue

                new_cr = m.group(1)

                if new_cr == old_cr:
                    if new_name != old_name and new_name:
                        conn.exec_driver_sql("UPDATE patients SET patient_name = ? WHERE cr_number = ?", (new_name, old_cr))
                    continue

                # Target new_cr is different from old_cr. Check if new_cr already exists:
                existing = conn.exec_driver_sql("SELECT cr_number FROM patients WHERE cr_number = ?", (new_cr,)).fetchone()
                if existing:
                    # Merge old_cr into new_cr
                    conn.exec_driver_sql("UPDATE scans SET cr_number = ? WHERE cr_number = ?", (new_cr, old_cr))
                    conn.exec_driver_sql("UPDATE custom_data SET cr_number = ? WHERE cr_number = ?", (new_cr, old_cr))

                    if 'patient_trials' in tables:
                        trials = conn.exec_driver_sql("SELECT trial_id FROM patient_trials WHERE patient_cr = ?", (old_cr,)).fetchall()
                        for (tid,) in trials:
                            has_t = conn.exec_driver_sql("SELECT 1 FROM patient_trials WHERE patient_cr = ? AND trial_id = ?", (new_cr, tid)).fetchone()
                            if has_t:
                                conn.exec_driver_sql("DELETE FROM patient_trials WHERE patient_cr = ? AND trial_id = ?", (old_cr, tid))
                            else:
                                conn.exec_driver_sql("UPDATE patient_trials SET patient_cr = ? WHERE patient_cr = ? AND trial_id = ?", (new_cr, old_cr, tid))

                    if 'trial_patient_data' in tables:
                        tpds = conn.exec_driver_sql("SELECT id, trial_id FROM trial_patient_data WHERE cr_number = ?", (old_cr,)).fetchall()
                        for (tpd_id, tid) in tpds:
                            has_tpd = conn.exec_driver_sql("SELECT 1 FROM trial_patient_data WHERE cr_number = ? AND trial_id = ?", (new_cr, tid)).fetchone()
                            if has_tpd:
                                conn.exec_driver_sql("DELETE FROM trial_patient_data WHERE id = ?", (tpd_id,))
                            else:
                                conn.exec_driver_sql("UPDATE trial_patient_data SET cr_number = ? WHERE id = ?", (new_cr, tpd_id))

                    conn.exec_driver_sql("DELETE FROM patients WHERE cr_number = ?", (old_cr,))
                else:
                    # Rename old_cr to new_cr across all tables
                    conn.exec_driver_sql("UPDATE scans SET cr_number = ? WHERE cr_number = ?", (new_cr, old_cr))
                    conn.exec_driver_sql("UPDATE custom_data SET cr_number = ? WHERE cr_number = ?", (new_cr, old_cr))
                    if 'patient_trials' in tables:
                        conn.exec_driver_sql("UPDATE patient_trials SET patient_cr = ? WHERE patient_cr = ?", (new_cr, old_cr))
                    if 'trial_patient_data' in tables:
                        conn.exec_driver_sql("UPDATE trial_patient_data SET cr_number = ? WHERE cr_number = ?", (new_cr, old_cr))
                    conn.exec_driver_sql("UPDATE patients SET cr_number = ?, patient_name = ? WHERE cr_number = ?", (new_cr, new_name, old_cr))

                migrated_count += 1

            conn.commit()
            return migrated_count

        if engine_or_conn:
            return do_migration(engine_or_conn)
        else:
            with db.engine.connect() as conn:
                return do_migration(conn)
    except Exception as e:
        print(f"[Startup Migration Warning] Retroactive CR migration error: {e}")
        return 0


def ensure_patient_schema_migrations():
    """Lightweight startup migration script using ALTER TABLE to safely add
    diagnosis, ao_trauma_score, ao_grade, anatomy, and scanogram columns to the existing SQLite database
    without dropping or modifying existing patient records, and runs the retroactive 12-digit CR migration.
    """
    try:
        with db.engine.connect() as conn:
            tables = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if 'patients' in tables:
                pcols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(patients)").fetchall()}
                if 'diagnosis' not in pcols:
                    conn.exec_driver_sql("ALTER TABLE patients ADD COLUMN diagnosis TEXT")
                if 'ao_trauma_score' not in pcols:
                    conn.exec_driver_sql("ALTER TABLE patients ADD COLUMN ao_trauma_score VARCHAR(64)")
                if 'ao_grade' not in pcols:
                    conn.exec_driver_sql("ALTER TABLE patients ADD COLUMN ao_grade VARCHAR(64)")
                    if 'ao_trauma_score' in pcols:
                        conn.exec_driver_sql("UPDATE patients SET ao_grade = ao_trauma_score WHERE ao_grade IS NULL")
                if 'anatomy' not in pcols:
                    conn.exec_driver_sql("ALTER TABLE patients ADD COLUMN anatomy VARCHAR(128)")
                if 'scanogram' not in pcols:
                    conn.exec_driver_sql("ALTER TABLE patients ADD COLUMN scanogram VARCHAR(64) DEFAULT 'No'")
                conn.commit()

            # Execute retroactive 12-digit CR and name cleaning migration
            run_retroactive_cr_migration(conn)
    except Exception as e:
        print(f"[Startup Migration Warning] Safe patient schema migration error: {e}")


def trigger_background_auto_sync(app_instance):
    r"""Automatically trigger a background recursive crawl of the local radiology directory
    (D:\RADIOLOGY DATA) the moment the server starts, without blocking the main web server thread.
    """
    def launcher():
        import time
        # Grace period for Flask server to bind ports and start serving
        time.sleep(2)
        target = LOCAL_DRIVE_PATH if os.path.exists(LOCAL_DRIVE_PATH) else DEFAULT_DRIVE_FOLDER_ID
        start_background_crawler(app_instance, target)

    sync_thread = threading.Thread(target=launcher, daemon=True, name="MasterRadiologyAutoSyncLauncher")
    sync_thread.start()


with app.app_context():
    ensure_patient_schema_migrations()
    seed_database_and_samples(app, db, {
        'User': User,
        'Patient': Patient,
        'Scan': Scan,
        'Trial': Trial,
        'CustomData': CustomData,
        'TrialPatientData': TrialPatientData
    })
    # NOTE: Background crawler auto-sync on server startup is disabled for clinical deployment.
    # The DICOM extraction engine ONLY runs when explicitly triggered via the "Ingest DICOM" or "Drive Sync" UI buttons.


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
