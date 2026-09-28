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

        # 1. Seed Users (Admin/PI vs Viewer)
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='Admin')
            admin.set_password('admin123')
            db.session.add(admin)

        if not User.query.filter_by(username='viewer').first():
            viewer = User(username='viewer', role='Viewer')
            viewer.set_password('viewer123')
            db.session.add(viewer)

        # 2. Seed Trials with EDC Schemas and Modality Constraints
        trial_tka = Trial.query.filter_by(trial_name='TOTAL-KNEE-2026').first()
        if not trial_tka:
            trial_tka = Trial(
                trial_name='TOTAL-KNEE-2026',
                description='Prospective multicenter registry evaluating cementless total knee arthroplasty alignment and implant longevity.',
                target_sample_size=60
            )
            trial_tka.set_required_modalities(['DX', 'CR', 'CT'])
            trial_tka.set_data_schema([
                {'name': 'Kellgren-Lawrence Grade', 'type': 'Text'},
                {'name': 'Pre-Op Knee Society Score (KSS)', 'type': 'Number'},
                {'name': 'Surgery Date', 'type': 'Date'},
                {'name': 'Implant Specifications Sheet', 'type': 'File'}
            ])
            db.session.add(trial_tka)
        else:
            if not trial_tka.target_sample_size:
                trial_tka.target_sample_size = 60
            if not trial_tka.get_required_modalities():
                trial_tka.set_required_modalities(['DX', 'CR', 'CT'])
            if not trial_tka.get_data_schema():
                trial_tka.set_data_schema([
                    {'name': 'Kellgren-Lawrence Grade', 'type': 'Text'},
                    {'name': 'Pre-Op Knee Society Score (KSS)', 'type': 'Number'},
                    {'name': 'Surgery Date', 'type': 'Date'},
                    {'name': 'Implant Specifications Sheet', 'type': 'File'}
                ])

        trial_femur = Trial.query.filter_by(trial_name='DISTAL-FEMUR-FX').first()
        if not trial_femur:
            trial_femur = Trial(
                trial_name='DISTAL-FEMUR-FX',
                description='Comparative evaluation of dual-plating versus lateral locking plate in distal femoral fragility fractures.',
                target_sample_size=40
            )
            trial_femur.set_required_modalities(['CR', 'CT'])
            trial_femur.set_data_schema([
                {'name': 'Fracture AO/OTA Classification', 'type': 'Text'},
                {'name': 'Bone Mineral Density T-Score', 'type': 'Number'},
                {'name': 'Injury Date', 'type': 'Date'},
                {'name': 'Post-Op X-Ray Report', 'type': 'File'}
            ])
            db.session.add(trial_femur)
        else:
            if not trial_femur.target_sample_size:
                trial_femur.target_sample_size = 40
            if not trial_femur.get_required_modalities():
                trial_femur.set_required_modalities(['CR', 'CT'])
            if not trial_femur.get_data_schema():
                trial_femur.set_data_schema([
                    {'name': 'Fracture AO/OTA Classification', 'type': 'Text'},
                    {'name': 'Bone Mineral Density T-Score', 'type': 'Number'},
                    {'name': 'Injury Date', 'type': 'Date'},
                    {'name': 'Post-Op X-Ray Report', 'type': 'File'}
                ])

        trial_spine = Trial.query.filter_by(trial_name='SCOLIOSIS-COBB-ALIGN').first()
        if not trial_spine:
            trial_spine = Trial(
                trial_name='SCOLIOSIS-COBB-ALIGN',
                description='Pre- and post-operative radiographic Cobb angle tracking in adolescent idiopathic scoliosis.',
                target_sample_size=50
            )
            trial_spine.set_required_modalities(['CR', 'SR'])
            trial_spine.set_data_schema([
                {'name': 'Primary Curve Cobb Angle', 'type': 'Number'},
                {'name': 'Risser Sign', 'type': 'Text'},
                {'name': 'Radiographic Assessment Date', 'type': 'Date'},
                {'name': 'Orthoroentgenogram Analysis File', 'type': 'File'}
            ])
            db.session.add(trial_spine)
        else:
            if not trial_spine.target_sample_size:
                trial_spine.target_sample_size = 50
            if not trial_spine.get_required_modalities():
                trial_spine.set_required_modalities(['CR', 'SR'])
            if not trial_spine.get_data_schema():
                trial_spine.set_data_schema([
                    {'name': 'Primary Curve Cobb Angle', 'type': 'Number'},
                    {'name': 'Risser Sign', 'type': 'Text'},
                    {'name': 'Radiographic Assessment Date', 'type': 'Date'},
                    {'name': 'Orthoroentgenogram Analysis File', 'type': 'File'}
                ])

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
                'trial': trial_femur,
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
                'trial': trial_tka,
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
                'trial': trial_spine,
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
                'trial': trial_femur,
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
                    gender=s['sex']
                )
                if s.get('trial') and s['trial'] not in p.trials:
                    p.trials.append(s['trial'])
                db.session.add(p)
                db.session.flush()

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
