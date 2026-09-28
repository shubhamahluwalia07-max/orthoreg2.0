import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

# Association tables for Many-to-Many relationships
patient_trials = db.Table(
    'patient_trials',
    db.Column('patient_cr', db.String(64), db.ForeignKey('patients.cr_number', ondelete='CASCADE'), primary_key=True),
    db.Column('trial_id', db.Integer, db.ForeignKey('trials.id', ondelete='CASCADE'), primary_key=True),
    db.Column('assigned_at', db.DateTime, default=utc_now)
)

scan_trials = db.Table(
    'scan_trials',
    db.Column('scan_id', db.Integer, db.ForeignKey('scans.id', ondelete='CASCADE'), primary_key=True),
    db.Column('trial_id', db.Integer, db.ForeignKey('trials.id', ondelete='CASCADE'), primary_key=True),
    db.Column('assigned_at', db.DateTime, default=utc_now)
)


class User(UserMixin, db.Model):
    """User model for Role-Based Access Control (Admin/PI vs Viewer)."""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='Viewer')  # 'Admin', 'PI', 'Viewer'
    created_at = db.Column(db.DateTime, default=utc_now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin_or_pi(self):
        return self.role in ['Admin', 'PI']

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


class Patient(db.Model):
    """Patient model identified by Central Registration (CR) Number."""
    __tablename__ = 'patients'

    cr_number = db.Column(db.String(64), primary_key=True, index=True)
    patient_name = db.Column(db.String(120), nullable=False, default='Anonymous')
    age = db.Column(db.String(20), nullable=True)
    gender = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    # Relationships
    scans = db.relationship('Scan', backref='patient', lazy=True, cascade='all, delete-orphan')
    custom_data = db.relationship('CustomData', backref='patient', lazy=True, cascade='all, delete-orphan')
    trials = db.relationship('Trial', secondary=patient_trials, backref=db.backref('patients', lazy='dynamic'))

    @property
    def series(self):
        """Alias returning grouped radiology series for this patient."""
        return self.scans

    def __repr__(self):
        return f'<Patient {self.cr_number}: {self.patient_name}>'


class Scan(db.Model):
    """Scan / Series model storing metadata, series grouping, and local / cloud file pointers."""
    __tablename__ = 'scans'

    id = db.Column(db.Integer, primary_key=True)
    cr_number = db.Column(db.String(64), db.ForeignKey('patients.cr_number', ondelete='CASCADE'), nullable=False, index=True)
    local_file_path = db.Column(db.String(512), nullable=True, index=True)  # Full path of primary slice on local drive
    drive_file_id = db.Column(db.String(128), nullable=True, index=True)    # Optional remote Google Drive file ID
    file_name = db.Column(db.String(255), nullable=True)
    file_size_bytes = db.Column(db.BigInteger, nullable=True)
    modality = db.Column(db.String(32), nullable=True, default='CR')        # CR, DX, CT, MR, etc.
    date_of_test = db.Column(db.String(32), nullable=True)                  # DICOM StudyDate e.g. YYYY-MM-DD
    series_description = db.Column(db.String(255), nullable=True)
    study_instance_uid = db.Column(db.String(128), nullable=True)
    series_instance_uid = db.Column(db.String(128), nullable=True, index=True) # Unique Series UID
    sop_instance_uid = db.Column(db.String(128), nullable=True)
    instance_count = db.Column(db.Integer, nullable=False, default=1)       # Total slices / images in this series
    instance_files = db.Column(db.Text, nullable=True)                      # JSON array of file paths in this series
    created_at = db.Column(db.DateTime, default=utc_now)

    # Many-to-Many with Trial
    trials = db.relationship('Trial', secondary=scan_trials, backref=db.backref('scans', lazy='dynamic'))

    @property
    def local_storage_path(self):
        """Backward compatibility alias for local_file_path."""
        return self.local_file_path

    @local_storage_path.setter
    def local_storage_path(self, value):
        self.local_file_path = value

    def get_instance_files(self):
        """Return list of individual file paths in this series."""
        import json
        if self.instance_files:
            try:
                return json.loads(self.instance_files)
            except Exception:
                pass
        return [self.local_file_path] if self.local_file_path else []

    def add_instance_file(self, path):
        """Register an additional slice file path to this series."""
        import json
        files = self.get_instance_files()
        if path not in files:
            files.append(path)
            self.instance_files = json.dumps(files)
            self.instance_count = len(files)

    def __repr__(self):
        return f'<Scan {self.id} (CR: {self.cr_number}, Modality: {self.modality}, Slices: {self.instance_count}, SeriesUID: {self.series_instance_uid})>'


# Series alias for Scan model to support series-oriented workflows
Series = Scan


class Trial(db.Model):
    """Clinical Trial or Research Cohort model with Dynamic EDC Schema and Modality Constraints."""
    __tablename__ = 'trials'

    id = db.Column(db.Integer, primary_key=True)
    trial_name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    target_sample_size = db.Column(db.Integer, nullable=True, default=50)
    required_modalities = db.Column(db.Text, nullable=True, default='[]')  # JSON array of modalities e.g. ["CT", "CR"]
    data_schema = db.Column(db.Text, nullable=True, default='[]')          # JSON array of fields e.g. [{"name": "X", "type": "Text"}]
    created_at = db.Column(db.DateTime, default=utc_now)

    def get_required_modalities(self):
        """Return list of required modality strings for this trial protocol."""
        import json
        if self.required_modalities:
            try:
                res = json.loads(self.required_modalities)
                if isinstance(res, list):
                    return res
            except Exception:
                return [m.strip().upper() for m in self.required_modalities.split(',') if m.strip()]
        return []

    def set_required_modalities(self, modalities):
        import json
        if isinstance(modalities, list):
            self.required_modalities = json.dumps([m.strip().upper() for m in modalities if m.strip()])
        else:
            self.required_modalities = json.dumps([])

    def get_data_schema(self):
        """Return list of dynamic EDC field definitions: [{'name': '...', 'type': 'Text|Number|Date|File'}]"""
        import json
        if self.data_schema:
            try:
                res = json.loads(self.data_schema)
                if isinstance(res, list):
                    return res
            except Exception:
                pass
        return []

    def set_data_schema(self, schema_list):
        import json
        if isinstance(schema_list, list):
            self.data_schema = json.dumps(schema_list)
        else:
            self.data_schema = json.dumps([])

    def __repr__(self):
        return f'<Trial {self.trial_name}>'


class TrialPatientData(db.Model):
    """Dynamic EDC data for a Patient enrolled in a specific Trial.
    Stores clinical trial parameters according to the trial's data_schema.
    """
    __tablename__ = 'trial_patient_data'

    id = db.Column(db.Integer, primary_key=True)
    trial_id = db.Column(db.Integer, db.ForeignKey('trials.id', ondelete='CASCADE'), nullable=False, index=True)
    cr_number = db.Column(db.String(64), db.ForeignKey('patients.cr_number', ondelete='CASCADE'), nullable=False, index=True)
    data_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    trial = db.relationship('Trial', backref=db.backref('patient_data_records', lazy=True, cascade='all, delete-orphan'))
    patient = db.relationship('Patient', backref=db.backref('trial_data_records', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('trial_id', 'cr_number', name='uq_trial_patient_data'),
    )

    def get_data(self):
        import json
        if self.data_json:
            try:
                val = json.loads(self.data_json)
                if isinstance(val, dict):
                    return val
            except Exception:
                pass
        return {}

    def set_data(self, data_dict):
        import json
        if isinstance(data_dict, dict):
            self.data_json = json.dumps(data_dict)
        else:
            self.data_json = json.dumps({})

    def __repr__(self):
        return f'<TrialPatientData Trial:{self.trial_id} Patient:{self.cr_number}>'


class CustomData(db.Model):
    """Dynamic custom columns linked to the Patient.
    Can store text strings, hyperlinks, or file paths for PDFs/images.
    """
    __tablename__ = 'custom_data'

    id = db.Column(db.Integer, primary_key=True)
    cr_number = db.Column(db.String(64), db.ForeignKey('patients.cr_number', ondelete='CASCADE'), nullable=False, index=True)
    field_name = db.Column(db.String(100), nullable=False)
    field_type = db.Column(db.String(20), nullable=False, default='text')  # 'text', 'link', 'file'
    field_value = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<CustomData {self.field_name}={self.field_value[:20]} (Patient: {self.cr_number})>'

