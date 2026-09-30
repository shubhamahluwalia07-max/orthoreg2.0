import os
import sys
import datetime
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
from werkzeug.security import generate_password_hash

def create_synthetic_dicom(filepath, patient_name, cr_number, modality, age, sex, study_date, series_desc, shape=(512, 512), pattern_type='bone'):
    """Generate a valid synthetic orthopedic DICOM file with realistic pixel data and tags."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = Dataset()
    ds.file_meta = file_meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    # Identification tags
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientID = cr_number
    ds.PatientName = patient_name
    ds.PatientAge = age
    ds.PatientSex = sex
    ds.Modality = modality
    ds.StudyDate = study_date
    ds.SeriesDate = study_date
    ds.SeriesDescription = series_desc
    ds.StudyDescription = f"Orthopedic Evaluation - {series_desc}"
    ds.InstanceNumber = 1

    # Image Pixel Module
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = shape[0]
    ds.Columns = shape[1]
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0  # Unsigned integer

    # Clinical Window / Level defaults
    if pattern_type == 'bone':
        ds.WindowCenter = "400"
        ds.WindowWidth = "2000"
    else:
        ds.WindowCenter = "40"
        ds.WindowWidth = "400"

    # Generate synthetic orthopedic-like radiograph image array (e.g. bone shaft or joint contrast)
    rows, cols = shape
    img = np.zeros((rows, cols), dtype=np.uint16) + 150  # Soft tissue background

    y, x = np.ogrid[:rows, :cols]
    cx, cy = cols // 2, rows // 2

    if pattern_type == 'bone':
        # Bone cortical shaft in center with high attenuation
        shaft_mask = (np.abs(x - cx) < 60) & (y > 50) & (y < rows - 50)
        img[shaft_mask] = 1800  # Dense cortical bone
        # Trabecular interior
        trabecular_mask = (np.abs(x - cx) < 35) & (y > 70) & (y < rows - 70)
        img[trabecular_mask] = 1200
        # Simulated hairline fracture line for clinical demonstration
        fracture_mask = (y - 250 == np.int32(0.5 * (x - cx))) & (np.abs(x - cx) < 55)
        img[fracture_mask] = 400
        # Add subtle anatomical gradient
        img += np.uint16(50 * np.sin(y / 40.0) + 50 * np.cos(x / 40.0))
    elif pattern_type == 'joint':
        # Joint space / rounded condyles
        condyle1 = ((x - (cx - 50))**2 + (y - (cy - 70))**2) < 4500
        condyle2 = ((x - (cx + 50))**2 + (y - (cy - 70))**2) < 4500
        plateau = (np.abs(x - cx) < 110) & (y > cy + 20) & (y < cy + 120)
        img[condyle1 | condyle2] = 1700
        img[plateau] = 1500
        # Implant pin / screw demonstration
        screw = (np.abs((x - 80) - 0.3 * y) < 8) & (y > cy) & (y < cy + 100)
        img[screw] = 2800  # High attenuation titanium implant
    else:
        # Spine vertebrae demo
        for v in range(3):
            vy = 120 + v * 120
            body = (np.abs(x - cx) < 70) & (np.abs(y - vy) < 35)
            img[body] = 1600

    ds.PixelData = img.tobytes()

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    pydicom.dcmwrite(filepath, ds, write_like_original=False)
    return filepath


def ensure_schema_migrations(db):
    """Ensure newly added columns exist in existing SQLite databases."""
    try:
        with db.engine.connect() as conn:
            tables = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if 'scans' in tables:
                cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(scans)").fetchall()}
                if 'local_file_path' not in cols:
                    conn.exec_driver_sql("ALTER TABLE scans ADD COLUMN local_file_path VARCHAR(512)")
                    if 'local_storage_path' in cols:
                        conn.exec_driver_sql("UPDATE scans SET local_file_path = local_storage_path WHERE local_file_path IS NULL")
                if 'file_size_bytes' not in cols:
                    conn.exec_driver_sql("ALTER TABLE scans ADD COLUMN file_size_bytes BIGINT")
                if 'series_instance_uid' not in cols:
                    conn.exec_driver_sql("ALTER TABLE scans ADD COLUMN series_instance_uid VARCHAR(128)")
                if 'instance_count' not in cols:
                    conn.exec_driver_sql("ALTER TABLE scans ADD COLUMN instance_count INTEGER DEFAULT 1")
                if 'instance_files' not in cols:
                    conn.exec_driver_sql("ALTER TABLE scans ADD COLUMN instance_files TEXT")

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

            if 'users' in tables:
                ucols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()}
                if 'email' not in ucols:
                    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN email VARCHAR(120)")
                    conn.exec_driver_sql("UPDATE users SET email = username || '@orthoreg.org' WHERE email IS NULL")
                if 'name' not in ucols:
                    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN name VARCHAR(120)")
                if 'status' not in ucols:
                    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN status VARCHAR(20) DEFAULT 'Active'")
                    conn.exec_driver_sql("UPDATE users SET status = 'Active' WHERE status IS NULL")
                if 'approved_at' not in ucols:
                    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN approved_at DATETIME")
                if 'must_change_password' not in ucols:
                    conn.exec_driver_sql("ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0")

                conn.exec_driver_sql("UPDATE users SET status = 'Active' WHERE status IS NULL")
                conn.exec_driver_sql("UPDATE users SET must_change_password = 0 WHERE must_change_password IS NULL")
                conn.exec_driver_sql("UPDATE users SET role = 'User' WHERE role IS NULL")
                conn.exec_driver_sql("UPDATE users SET email = username || '@orthoreg.org' WHERE email IS NULL AND username IS NOT NULL")
                conn.exec_driver_sql("UPDATE users SET username = email WHERE username IS NULL AND email IS NOT NULL")

            if 'trials' in tables:
                tcols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(trials)").fetchall()}
                if 'target_sample_size' not in tcols:
                    conn.exec_driver_sql("ALTER TABLE trials ADD COLUMN target_sample_size INTEGER DEFAULT 50")
                if 'required_modalities' not in tcols:
                    conn.exec_driver_sql("ALTER TABLE trials ADD COLUMN required_modalities TEXT DEFAULT '[]'")
                if 'data_schema' not in tcols:
                    conn.exec_driver_sql("ALTER TABLE trials ADD COLUMN data_schema TEXT DEFAULT '[]'")

            conn.commit()
    except Exception as e:
        print(f"[Migration Warning] Schema migration check: {e}")


def seed_database_and_samples(app, db, models):
    """Seed initial clinical users, demo clinical trials, and sample orthopedic DICOM files.
    Includes startup reset logic to drop existing database tables on the next startup for clean series grouping.
    """
    User = models['User']
    Patient = models['Patient']
    Scan = models['Scan']
    Trial = models['Trial']
    CustomData = models['CustomData']
    TrialPatientData = models.get('TrialPatientData')

    with app.app_context():
        # Check if database reset is required on this startup
        reset_sentinel = os.path.join(app.root_path, 'instance', '.series_migration_v2_done')
        force_reset = os.environ.get('RESET_DB_ON_STARTUP', '').lower() in ('true', '1', 'yes') or '--reset-db' in sys.argv

        if force_reset or not os.path.exists(reset_sentinel):
            print("\n" + "=" * 72)
            print("[Database Reset] Dropping existing database tables on startup for clean Series grouping...")
            print("=" * 72)
            try:
                db.drop_all()
                db.create_all()
                with open(reset_sentinel, 'w', encoding='utf-8') as f:
                    f.write(f"Series grouping v2 initialized at {datetime.datetime.now().isoformat()}\n")
                print("[Database Reset] Successfully dropped existing tables and initialized clean schema.")
            except Exception as e:
                print(f"[Database Reset Error] Failed during reset: {e}")
        else:
            db.create_all()
            ensure_schema_migrations(db)

        # 1. Seed Dynamic RBAC Users (Admin, Editor, User) & Pending Requests
        admin = User.query.filter((User.username == 'admin') | (User.email == 'admin@orthoreg.org')).first()
        if not admin:
            admin = User(
                email='admin@orthoreg.org',
                username='admin',
                name='Chief PI / Administrator',
                role='Admin',
                status='Active'
            )
            admin.set_password('admin123')
            db.session.add(admin)
        else:
            admin.role = 'Admin'
            admin.status = 'Active'
            if not admin.email:
                admin.email = 'admin@orthoreg.org'
            admin.set_password('admin123')

        editor = User.query.filter((User.username == 'editor') | (User.email == 'editor@orthoreg.org')).first()
        if not editor:
            editor = User(
                email='editor@orthoreg.org',
                username='editor',
                name='Clinical Research Editor',
                role='Editor',
                status='Active'
            )
            editor.set_password('editor123')
            db.session.add(editor)
        else:
            editor.role = 'Editor'
            editor.status = 'Active'
            if not editor.email:
                editor.email = 'editor@orthoreg.org'

        viewer = User.query.filter((User.username == 'viewer') | (User.email == 'viewer@orthoreg.org')).first()
        if not viewer:
            viewer = User(
                email='viewer@orthoreg.org',
                username='viewer',
                name='Clinical Radiology Viewer',
                role='User',
                status='Active'
            )
            viewer.set_password('viewer123')
            db.session.add(viewer)
        else:
            viewer.role = 'User'
            viewer.status = 'Active'
            if not viewer.email:
                viewer.email = 'viewer@orthoreg.org'

        # Seed a sample pending access request for demonstration in User Management panel
        pending_user = User.query.filter_by(email='dr.patel@hospital.org').first()
        if not pending_user:
            pending_user = User(
                email='dr.patel@hospital.org',
                username='dr.patel@hospital.org',
                name='Dr. Anita Patel',
                role='User',
                status='Pending'
            )
            pending_user.set_password('request123')
            db.session.add(pending_user)

        # 2. Halt Mock Data Injection: The trial registry must remain strictly empty until manually populated by the research team.
        # Clean up any previously seeded mock trials from older versions
        dummy_trial_names = ['TOTAL-KNEE-2026', 'DISTAL-FEMUR-FX', 'SCOLIOSIS-COBB-ALIGN']
        existing_dummy_trials = Trial.query.filter(Trial.trial_name.in_(dummy_trial_names)).all()
        for dt in existing_dummy_trials:
            dt.patients = []
            db.session.delete(dt)
        if existing_dummy_trials:
            db.session.commit()

        # 3. Create Sample Synthetic DICOMs in sample_dicoms/ folder if not yet created
        sample_dir = os.path.join(app.root_path, 'sample_dicoms')
        os.makedirs(sample_dir, exist_ok=True)

        samples_meta = [
            {
                'file': 'femur_fx_001.dcm',
                'name': 'SHARMA^RAJESH CR100921',
                'cr': 'CR100921',
                'modality': 'CR',
                'age': '058Y',
                'sex': 'M',
                'date': '20260814',
                'desc': 'Right Femur AP/Lateral Radiograph',
                'pattern': 'bone',
                'trial': None,
                'diagnosis': 'Distal Femur Extra-Articular Fracture',
                'ao_score': '33-A2',
                'anatomy': 'Distal Femur',
                'custom': [('Fracture AO/OTA Classification', 'text', '33-A2 Simple Extra-articular'),
                           ('Post-Op X-Ray Report', 'link', 'https://radiopaedia.org/articles/distal-femoral-fracture')]
            },
            {
                'file': 'knee_tka_002.dcm',
                'name': 'PATEL^SUNITA',
                'cr': 'CR104582',
                'modality': 'DX',
                'age': '064Y',
                'sex': 'F',
                'date': '20260901',
                'desc': 'Left Knee Weight-Bearing AP View',
                'pattern': 'joint',
                'trial': None,
                'diagnosis': 'Bilateral Knee Tricompartmental Osteoarthritis',
                'ao_score': 'N/A (Degenerative)',
                'anatomy': 'Knee Joint',
                'custom': [('Kellgren-Lawrence Grade', 'text', 'Grade 4 (Severe OA)'),
                           ('Pre-op Knee Society Score (KSS)', 'text', '42 / 100')]
            },
            {
                'file': 'spine_scoliosis_003.dcm',
                'name': 'VERMA^AARAV CR205819',
                'cr': 'CR205819',
                'modality': 'CR',
                'age': '016Y',
                'sex': 'M',
                'date': '20260910',
                'desc': 'Full Spine Standing Orthoroentgenogram',
                'pattern': 'spine',
                'trial': None,
                'diagnosis': 'Adolescent Idiopathic Scoliosis',
                'ao_score': 'N/A (Deformity)',
                'anatomy': 'Thoracolumbar Spine',
                'custom': [('Primary Curve Cobb Angle', 'text', '38.4 degrees Thoracic Right'),
                           ('Risser Sign', 'text', 'Stage 3')]
            },
            {
                'file': 'hip_pelvis_004.dcm',
                'name': 'GUPTA^MEENA',
                'cr': 'CR308412',
                'modality': 'CR',
                'age': '072Y',
                'sex': 'F',
                'date': '20260918',
                'desc': 'Pelvis with Both Hips AP View',
                'pattern': 'bone',
                'trial': None,
                'diagnosis': 'Displaced Subcapital Femoral Neck Fracture',
                'ao_score': '31-B2',
                'anatomy': 'Proximal Femur / Hip',
                'custom': [('Bone Mineral Density T-Score', 'text', '-3.1 (Severe Osteoporosis)')]
            }
        ]

        for s in samples_meta:
            filepath = os.path.join(sample_dir, s['file'])
            if not os.path.exists(filepath):
                create_synthetic_dicom(
                    filepath=filepath,
                    patient_name=s['name'],
                    cr_number=s['cr'],
                    modality=s['modality'],
                    age=s['age'],
                    sex=s['sex'],
                    study_date=s['date'],
                    series_desc=s['desc'],
                    pattern_type=s['pattern']
                )

            # Check if patient exists
            p = Patient.query.filter_by(cr_number=s['cr']).first()
            if not p:
                from drive_service import clean_patient_name, clean_age
                p = Patient(
                    cr_number=s['cr'],
                    patient_name=clean_patient_name(s['name']),
                    age=clean_age(s['age']),
                    gender=s['sex'],
                    diagnosis=s.get('diagnosis'),
                    ao_trauma_score=s.get('ao_score'),
                    anatomy=s.get('anatomy')
                )
                if s.get('trial') and s['trial'] not in p.trials:
                    p.trials.append(s['trial'])
                db.session.add(p)
                db.session.flush()
            else:
                if not p.diagnosis and s.get('diagnosis'):
                    p.diagnosis = s['diagnosis']
                if not p.ao_trauma_score and s.get('ao_score'):
                    p.ao_trauma_score = s['ao_score']
                if not p.anatomy and s.get('anatomy'):
                    p.anatomy = s['anatomy']

            # Check if scan exists
            sc = Scan.query.filter_by(cr_number=p.cr_number, file_name=s['file']).first()
            if not sc:
                import json
                sc = Scan(
                    cr_number=p.cr_number,
                    drive_file_id=f"local_{s['file']}",
                    file_name=s['file'],
                    modality=s['modality'],
                    date_of_test=f"{s['date'][:4]}-{s['date'][4:6]}-{s['date'][6:]}",
                    series_description=s['desc'],
                    local_file_path=filepath,
                    file_size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else 0,
                    series_instance_uid=f"1.2.826.0.1.3680043.8.498.{s['cr'].replace('CR', '')}",
                    instance_count=1,
                    instance_files=json.dumps([filepath])
                )
                if s.get('trial') and s['trial'] not in sc.trials:
                    sc.trials.append(s['trial'])
                db.session.add(sc)

            # Add Custom Data fields if not present
            for c_name, c_type, c_val in s.get('custom', []):
                cd = CustomData.query.filter_by(cr_number=p.cr_number, field_name=c_name).first()
                if not cd:
                    cd = CustomData(
                        cr_number=p.cr_number,
                        field_name=c_name,
                        field_type=c_type,
                        field_value=c_val
                    )
                    db.session.add(cd)

            # Seed TrialPatientData if trial is assigned
            if TrialPatientData and s.get('trial'):
                tr = s['trial']
                tpd = TrialPatientData.query.filter_by(trial_id=tr.id, cr_number=p.cr_number).first()
                if not tpd:
                    tpd = TrialPatientData(trial_id=tr.id, cr_number=p.cr_number)
                    db.session.add(tpd)
                cur_data = tpd.get_data()
                for c_name, c_type, c_val in s.get('custom', []):
                    if c_name not in cur_data:
                        cur_data[c_name] = c_val
                tpd.set_data(cur_data)

        db.session.commit()
        print("[DB Seed] Successfully verified database seed data and orthopedic DICOMs.")

    # Ensure Cornerstone Web Worker & Codecs are present in static/js/
    ensure_cornerstone_worker_assets(app.root_path)


def ensure_cornerstone_worker_assets(root_dir):
    """Automatically download cornerstoneWADOImageLoaderWebWorker.js and cornerstoneWADOImageLoaderCodecs.js into static/js/ if missing."""
    import urllib.request
    js_dir = os.path.join(root_dir, 'static', 'js')
    os.makedirs(js_dir, exist_ok=True)

    assets = {
        'cornerstoneWADOImageLoaderWebWorker.js': 'https://unpkg.com/cornerstone-wado-image-loader@3.1.2/dist/cornerstoneWADOImageLoaderWebWorker.js',
        'cornerstoneWADOImageLoaderCodecs.js': 'https://unpkg.com/cornerstone-wado-image-loader@2.2.3/dist/cornerstoneWADOImageLoaderCodecs.js',
    }

    for fname, url in assets.items():
        target = os.path.join(js_dir, fname)
        if not os.path.exists(target) or os.path.getsize(target) == 0:
            try:
                print(f"[Setup] Downloading Cornerstone asset '{fname}' into static/js/...")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp, open(target, 'wb') as out_f:
                    out_f.write(resp.read())
                print(f"[Setup] Successfully saved '{fname}' ({os.path.getsize(target)} bytes).")
            except Exception as e:
                print(f"[Setup] Notice: Could not download '{fname}': {e}")
