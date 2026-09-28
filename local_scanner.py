import os
import re
import datetime
import pydicom
from pydicom.errors import InvalidDicomError
from drive_service import extract_cr_number, clean_patient_name, clean_age, clean_study_date

# Directories and extensions to ignore during recursive walk
EXCLUDED_DIR_NAMES = {
    '.tmp.driveupload', '$recycle.bin', 'system volume information',
    '.git', '__pycache__', '.idea', '.vscode'
}
EXCLUDED_EXTENSIONS = {
    '.ini', '.mcs', '.txt', '.log', '.tmp', '.exe', '.dll', '.zip',
    '.rar', '.7z', '.pdf', '.png', '.jpg', '.jpeg', '.csv', '.xlsx'
}


def is_dicom_file(filepath):
    """Fast check whether a local file is a valid DICOM file.
    Checks file extension first (.dcm), or verifies the standard 'DICM' preamble at byte offset 128.
    """
    fname = os.path.basename(filepath)
    ext = os.path.splitext(fname)[1].lower()

    if ext in EXCLUDED_EXTENSIONS:
        return False

    if ext in ('.dcm', '.dicom'):
        return True

    # For files without .dcm extension (common in raw clinical exports), check 128-byte preamble
    try:
        if os.path.getsize(filepath) >= 132:
            with open(filepath, 'rb') as f:
                f.seek(128)
                return f.read(4) == b'DICM'
    except Exception:
        return False

    return False


def extract_metadata_from_file(filepath):
    """Instantly parse clinical metadata from a local DICOM file using stop_before_pixels=True.
    Skips the heavy pixel array completely for maximum speed.
    Extracts SeriesInstanceUID for series grouping.
    """
    try:
        ds = pydicom.dcmread(filepath, stop_before_pixels=True, force=True)
    except InvalidDicomError:
        raise ValueError("Invalid DICOM header or corrupted file format")

    raw_pid = getattr(ds, 'PatientID', '')
    raw_pname = getattr(ds, 'PatientName', '')
    raw_age = getattr(ds, 'PatientAge', '')
    raw_birth_date = getattr(ds, 'PatientBirthDate', '')
    raw_sex = getattr(ds, 'PatientSex', 'O')
    raw_modality = getattr(ds, 'Modality', 'CR')
    raw_date = getattr(ds, 'StudyDate', '')
    series_desc = getattr(ds, 'SeriesDescription', getattr(ds, 'StudyDescription', 'Orthopedic Scan'))
    study_uid = str(getattr(ds, 'StudyInstanceUID', '') or '')
    series_uid = str(getattr(ds, 'SeriesInstanceUID', '') or getattr(ds, 'SeriesInstanceUid', '') or '').strip()
    sop_uid = str(getattr(ds, 'SOPInstanceUID', '') or '')
    instance_num = getattr(ds, 'InstanceNumber', 1)

    parent_folder = os.path.basename(os.path.dirname(filepath))
    cr_number = extract_cr_number(raw_pid, raw_pname, fallback_uid=study_uid or sop_uid, filepath=filepath)
    patient_name = clean_patient_name(raw_pname, fallback=parent_folder)
    study_date = clean_study_date(raw_date)
    age = clean_age(raw_age, birth_date=raw_birth_date, study_date=study_date)

    # Normalize gender
    sex_str = str(raw_sex).strip().upper()
    if sex_str.startswith('M'):
        gender = 'M'
    elif sex_str.startswith('F'):
        gender = 'F'
    else:
        gender = 'Other'

    modality = (str(raw_modality or 'CR')).strip().upper()

    try:
        file_size = os.path.getsize(filepath)
    except Exception:
        file_size = 0

    return {
        'cr_number': cr_number,
        'patient_name': patient_name or 'Anonymous',
        'age': age or 'Unknown',
        'gender': gender or 'Other',
        'modality': modality or 'CR',
        'date_of_test': study_date,
        'series_description': str(series_desc)[:250] if series_desc else 'Scan',
        'study_instance_uid': study_uid,
        'series_instance_uid': series_uid,
        'sop_instance_uid': sop_uid,
        'instance_number': instance_num,
        'raw_patient_id': str(raw_pid).strip(),
        'raw_patient_name': str(raw_pname).strip(),
        'raw_patient_age': str(raw_age).strip(),
        'local_file_path': os.path.abspath(filepath),
        'file_name': os.path.basename(filepath),
        'file_size_bytes': file_size
    }


def find_matching_patient(db_session, Patient, meta):
    """Smart cross-referencing: match extracted DICOM tags against existing patients in SQLite.
    1. Cross-reference primary CR Number match.
    2. Cross-reference raw PatientID tag match.
    3. Cross-reference clean PatientName + PatientAge match against existing manual patient records.
    """
    # 1. Direct CR Number match
    cr = meta.get('cr_number')
    if cr and cr not in ('CR-UNKNOWN', 'UNKNOWN', ''):
        p = db_session.query(Patient).filter_by(cr_number=cr).first()
        if p:
            return p

    # 2. Raw PatientID tag match
    raw_pid = str(meta.get('raw_patient_id') or '').strip()
    if raw_pid and raw_pid.lower() not in ('none', 'unknown', 'anonymous', ''):
        p = db_session.query(Patient).filter(
            (Patient.cr_number == raw_pid) |
            (Patient.cr_number == f"CR{raw_pid}") |
            (Patient.cr_number.ilike(f"%{raw_pid}%"))
        ).first()
        if p:
            return p

    # 3. PatientName + PatientAge match (for manual pre-scan entries)
    pname = (meta.get('patient_name') or '').strip()
    age = (meta.get('age') or '').strip()
    if pname and pname not in ('Anonymous', 'Unknown Patient', 'Unknown'):
        age_num_match = re.search(r'\d+', age)
        age_num = age_num_match.group(0) if age_num_match else None

        candidates = db_session.query(Patient).filter(Patient.patient_name.ilike(pname)).all()
        for cand in candidates:
            cand_age = cand.age or ''
            cand_age_num_match = re.search(r'\d+', cand_age)
            cand_age_num = cand_age_num_match.group(0) if cand_age_num_match else None

            if age_num and cand_age_num:
                if age_num == cand_age_num:
                    return cand
            else:
                return cand

    return None


def scan_local_directory(root_directory, db_session, models, on_progress=None, drive_service=None):
    """Recursively search local directory tree using os.walk(), parse DICOM metadata with
    stop_before_pixels=True, and populate SQLite database with local_file_path.
    Dynamically reports progress during continuous local directory traversal.
    Cross-references existing manual patients and Google Drive cloud file_ids.
    """
    root_directory = os.path.abspath(root_directory)
    if not os.path.exists(root_directory):
        raise FileNotFoundError(f"Target local radiology directory does not exist: {root_directory}")

    print(f"\n[Local Crawler] Starting continuous local crawl in: {root_directory}")

    Patient = models['Patient']
    Scan = models['Scan']

    scanned_dirs_count = 0
    files_scanned_count = 0
    dicoms_found = 0
    ingested_count = 0
    skipped_count = 0
    errors = []

    for current_root, dirs, files in os.walk(root_directory):
        # Prune excluded directories in-place to avoid unnecessary traversals
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_DIR_NAMES and not d.startswith('.')]
        scanned_dirs_count += 1

        if on_progress:
            on_progress({
                'folders_scanned': scanned_dirs_count,
                'files_scanned': files_scanned_count,
                'current': files_scanned_count,
                'total': files_scanned_count,
                'ingested': ingested_count,
                'skipped': skipped_count
            })

        for f in files:
            if f.startswith('.'):
                continue
            files_scanned_count += 1
            fpath = os.path.join(current_root, f)

            if not is_dicom_file(fpath):
                if on_progress:
                    on_progress({
                        'folders_scanned': scanned_dirs_count,
                        'files_scanned': files_scanned_count,
                        'current': files_scanned_count,
                        'total': files_scanned_count,
                        'ingested': ingested_count,
                        'skipped': skipped_count
                    })
                continue

            dicoms_found += 1

            # Check if scan already exists in SQLite
            existing_scan = db_session.query(Scan).filter_by(local_file_path=fpath).first()
            if existing_scan:
                skipped_count += 1
                if on_progress:
                    on_progress({
                        'folders_scanned': scanned_dirs_count,
                        'files_scanned': files_scanned_count,
                        'current': files_scanned_count,
                        'total': files_scanned_count,
                        'ingested': ingested_count,
                        'skipped': skipped_count
                    })
                continue

            fname = os.path.basename(fpath)
            try:
                meta = extract_metadata_from_file(fpath)

                # Smart Auto-Mapping: Cross-reference against existing patients in SQLite
                patient = find_matching_patient(db_session, Patient, meta)
                if not patient:
                    patient = Patient(
                        cr_number=meta['cr_number'],
                        patient_name=meta['patient_name'] or 'Unknown Patient',
                        age=meta['age'] or 'Unknown',
                        gender=meta['gender'] or 'Other'
                    )
                    db_session.add(patient)
                    db_session.flush()
                else:
                    # Update patient fields if manual pre-scan entry had placeholders
                    if (not patient.patient_name or patient.patient_name in ('Anonymous', 'Unknown Patient', 'Unknown')) and meta['patient_name'] not in ('Anonymous', 'Unknown Patient', 'Unknown'):
                        patient.patient_name = meta['patient_name']
                    if (not patient.age or patient.age in ('Unknown', 'N/A')) and meta['age'] not in ('Unknown', 'N/A'):
                        patient.age = meta['age']
                    if (not patient.gender or patient.gender in ('Other', 'Unknown')) and meta['gender'] not in ('Other', 'Unknown'):
                        patient.gender = meta['gender']

                # Hybrid Cloud Cross-Referencing: Check Google Drive API for corresponding cloud file_id
                cloud_drive_id = None
                if drive_service and drive_service.is_configured():
                    try:
                        cloud_drive_id = drive_service.find_file_id_by_filename(fname)
                    except Exception as e:
                        print(f"[Drive Lookup] {e}")

                drive_id = cloud_drive_id or f"local_{fname}"

                # Grouping into Series (Scan) Record by SeriesInstanceUID under matched patient
                series_uid = meta.get('series_instance_uid')
                existing_series = None
                if series_uid:
                    existing_series = db_session.query(Scan).filter_by(
                        cr_number=patient.cr_number,
                        series_instance_uid=series_uid
                    ).first()
                if not existing_series and not series_uid:
                    existing_series = db_session.query(Scan).filter_by(
                        cr_number=patient.cr_number,
                        modality=meta['modality'],
                        date_of_test=meta['date_of_test'],
                        series_description=meta['series_description']
                    ).first()

                if existing_series:
                    # File belongs to an existing series: register slice file and update instance count
                    existing_series.add_instance_file(meta['local_file_path'])
                    if meta.get('file_size_bytes'):
                        existing_series.file_size_bytes = (existing_series.file_size_bytes or 0) + meta['file_size_bytes']
                    if existing_series.series_description in ('Scan', 'Orthopedic Scan', '') and meta['series_description'] not in ('Scan', 'Orthopedic Scan', ''):
                        existing_series.series_description = meta['series_description']
                    if cloud_drive_id and existing_series.drive_file_id.startswith('local_'):
                        existing_series.drive_file_id = cloud_drive_id
                    db_session.commit()
                    skipped_count += 1
                    print(f"[Local Crawler] [{files_scanned_count} files] -> Grouped into Series #{existing_series.id} ({meta['modality']} - {existing_series.instance_count} slices) | Patient: {patient.cr_number} | File: '{fname}'")
                else:
                    import json
                    scan = Scan(
                        cr_number=patient.cr_number,
                        local_file_path=meta['local_file_path'],
                        drive_file_id=drive_id,
                        file_name=meta['file_name'],
                        file_size_bytes=meta['file_size_bytes'],
                        modality=meta['modality'],
                        date_of_test=meta['date_of_test'],
                        series_description=meta['series_description'],
                        study_instance_uid=meta['study_instance_uid'],
                        series_instance_uid=series_uid or f"SERIES_{int(datetime.datetime.now().timestamp())}_{fname}",
                        sop_instance_uid=meta['sop_instance_uid'],
                        instance_count=1,
                        instance_files=json.dumps([meta['local_file_path']])
                    )
                    db_session.add(scan)
                    db_session.commit()
                    ingested_count += 1
                    print(f"[Local Crawler] [{files_scanned_count} files] -> INGESTED NEW SERIES: CR={patient.cr_number} | Patient='{patient.patient_name}' | Modality={meta['modality']} | Slices=1 | DriveID={drive_id}")

                if on_progress:
                    on_progress({
                        'folders_scanned': scanned_dirs_count,
                        'files_scanned': files_scanned_count,
                        'current': files_scanned_count,
                        'total': files_scanned_count,
                        'ingested': ingested_count,
                        'skipped': skipped_count,
                        'last_patient': f"{patient.cr_number} - {patient.patient_name}"
                    })

            except Exception as e:
                db_session.rollback()
                err_msg = f"Failed to parse '{fpath}': {str(e)}"
                errors.append(err_msg)
                print(f"[Local Crawler] [{files_scanned_count} files scanned] -> ERROR: {err_msg}")
                if on_progress:
                    on_progress({
                        'folders_scanned': scanned_dirs_count,
                        'files_scanned': files_scanned_count,
                        'current': files_scanned_count,
                        'total': files_scanned_count,
                        'ingested': ingested_count,
                        'skipped': skipped_count
                    })

    print(f"[Local Crawler] Scan finished: {scanned_dirs_count} directories traversed, {files_scanned_count} files scanned ({dicoms_found} DICOMs), {ingested_count} newly ingested, {skipped_count} skipped, {len(errors)} error(s).\n")

    return {
        'total_found': dicoms_found,
        'scanned_dirs': scanned_dirs_count,
        'folders_scanned': scanned_dirs_count,
        'files_scanned': files_scanned_count,
        'ingested': ingested_count,
        'skipped': skipped_count,
        'errors': errors
    }
