import os
import io
import re
import datetime
import pydicom
from pydicom.errors import InvalidDicomError
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2 import service_account

import threading

# Scopes required to read Google Drive files and metadata
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

# Default master Google Drive Folder ID for clinical orthopedic radiology
DEFAULT_DRIVE_FOLDER_ID = "1eEnoJi0hYHgrPmtWJub0fA6W_oPOL2iR"


def extract_strict_12digit_cr(*values):
    r"""Search PatientName, PatientID, filepath, and candidate strings for a strict 12-digit CR number starting with 19 or 20 (e.g. 201806126956)."""
    for c in values:
        if not c:
            continue
        s = str(c).strip()
        # Direct 12-digit number starting with 19xx or 20xx
        m = re.search(r'\b((?:19|20)\d{10})\b', s)
        if m:
            return m.group(1)
        # CR-prefixed 12-digit number e.g. CR201806126956 or CR-201806126956
        m_cr = re.search(r'\bCR[ -]?((?:19|20)\d{10})\b', s, flags=re.IGNORECASE)
        if m_cr:
            return m_cr.group(1)
    return None


def extract_cr_number(patient_id_val, patient_name_val, fallback_uid='', filepath=''):
    r"""Extract clinical Central Registration (CR) number using strict 12-digit regex first,
    falling back to standard clinical cascades.
    The true CR number is strictly defined as a 12-digit sequence starting with a 4-digit year (e.g., regex \b(?:19|20)\d{10}\b).
    """
    id_str = str(patient_id_val).strip() if patient_id_val else ''
    name_str = str(patient_name_val).strip() if patient_name_val else ''
    fb_str = str(fallback_uid).strip() if fallback_uid else ''
    path_str = str(filepath).strip() if filepath else ''

    # 1. Strict 12-digit CR starting with 19xx/20xx in PatientID, PatientName, filepath, or fallback_uid
    strict_cr = extract_strict_12digit_cr(id_str, name_str, path_str, fb_str)
    if strict_cr:
        return strict_cr

    combined = f"{id_str} {name_str} {path_str}"

    # 2. Match explicit CR or UHID pattern
    m = re.search(r'\b(CR[ -]?[0-9]{4,12})\b', combined, re.IGNORECASE)
    if m:
        return re.sub(r'[\s-]', '', m.group(1)).upper()

    m_uhid = re.search(r'\b(UHID[ -]?[0-9]{4,12})\b', combined, re.IGNORECASE)
    if m_uhid:
        return re.sub(r'[\s-]', '', m_uhid.group(1)).upper()

    # 3. If PatientID is purely numeric or contains digits
    if id_str:
        if id_str.isdigit() and len(id_str) >= 4:
            return f"CR{id_str}"
        if re.match(r'^[A-Z0-9_\-]{4,20}$', id_str, re.IGNORECASE):
            return id_str.upper()

    # 4. Search for 5-10 digit numbers in the combined string
    m_num = re.search(r'\b([0-9]{5,10})\b', combined)
    if m_num:
        return f"CR{m_num.group(1)}"

    # 5. Fallback: use id_str if available, or generate a deterministic anon ID
    if id_str and id_str.lower() not in ('unknown', 'anonymous', 'none'):
        clean_id = re.sub(r'[^A-Z0-9]', '', id_str.upper())
        if clean_id:
            return clean_id if clean_id.startswith('CR') else f"CR{clean_id}"

    clean_fallback = re.sub(r'[^A-Z0-9]', '', str(fallback_uid).upper())
    return f"CR-ANON-{clean_fallback[:8]}" if clean_fallback else "CR-UNKNOWN"


def clean_patient_name(raw_name, fallback=''):
    """Convert DICOM PersonName (e.g. 'DOE^JOHN^^DR' or 'SHARMA^RAJESH') into readable format ('JOHN DOE' or 'RAJESH SHARMA').
    Strictly strips embedded 12-digit CR numbers, CR/UHID codes, and extraneous digits.
    Guarantees non-blank return value.
    """
    if not raw_name:
        if fallback:
            # Clean fallback from folder/file name
            fb = re.sub(r'[_^]+', ' ', str(fallback)).strip()
            fb = re.sub(r'(?:CR[ -]?)?\b(?:19|20)\d{10}\b', '', fb, flags=re.IGNORECASE).strip()
            fb = re.sub(r'\b(CR|UHID)[ -]?[0-9]{4,12}\b', '', fb, flags=re.IGNORECASE).strip()
            if fb:
                return fb.title()
        return 'Anonymous'

    s = str(raw_name).strip()
    if not s or s.lower() in ('none', 'null', 'nan', '""', "''"):
        return 'Anonymous'

    # Strip embedded 12-digit CR numbers entirely out of the remaining PatientName string
    s = re.sub(r'(?:CR[ -]?)?\b(?:19|20)\d{10}\b', '', s, flags=re.IGNORECASE).strip()
    # Strip embedded CR/UHID codes from patient name for clean display
    s = re.sub(r'\b(CR|UHID)[ -]?[0-9]{4,12}\b', '', s, flags=re.IGNORECASE).strip()
    # Strip trailing numeric IDs
    s = re.sub(r'\b[0-9]{5,10}\b', '', s).strip()

    parts = [p.strip() for p in s.split('^') if p.strip()]
    if not parts:
        return 'Anonymous'

    # In standard DICOM: Last^First^Middle^Prefix^Suffix
    if len(parts) == 1:
        res = parts[0]
    elif len(parts) == 2:
        res = f"{parts[1]} {parts[0]}"
    elif len(parts) >= 3:
        res = f"{' '.join(parts[1:])} {parts[0]}"
    else:
        res = " ".join(parts)

    res = re.sub(r'\s+', ' ', res).strip()
    return res if res else 'Anonymous'


def clean_age(raw_age, birth_date='', study_date=''):
    """Normalize DICOM age e.g., '045Y' -> '45 Yrs', '006M' -> '6 Mos'.
    If raw_age is missing/unknown but birth_date and study_date are provided, computes age.
    """
    if raw_age:
        s = str(raw_age).strip()
        m = re.match(r'^0*([0-9]+)\s*([YMWDymwd])?$', s)
        if m:
            num = m.group(1)
            unit = (m.group(2) or 'Y').upper()
            unit_map = {'Y': 'Yrs', 'M': 'Mos', 'W': 'Wks', 'D': 'Days'}
            return f"{num} {unit_map.get(unit, unit)}"
        if s and s.lower() not in ('unknown', 'none', 'null', 'nan'):
            return s

    # Fallback to computing age from birth_date (YYYYMMDD) and study_date (YYYYMMDD or YYYY-MM-DD)
    if birth_date and len(str(birth_date).strip()) == 8 and str(birth_date).strip().isdigit():
        try:
            b_str = str(birth_date).strip()
            b_year = int(b_str[:4])
            s_str = re.sub(r'[^0-9]', '', str(study_date or ''))
            if len(s_str) >= 4:
                s_year = int(s_str[:4])
                calc_age = s_year - b_year
                if 0 <= calc_age <= 125:
                    return f"{calc_age} Yrs"
        except Exception:
            pass

    return 'Unknown'


def clean_study_date(raw_date):
    """Format DICOM YYYYMMDD to YYYY-MM-DD."""
    if not raw_date:
        return datetime.date.today().strftime('%Y-%m-%d')
    s = str(raw_date).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def parse_dicom_bytes(dicom_bytes_or_buffer, fallback_name='file.dcm', drive_file_id='local', filepath=''):
    """Read raw DICOM bytes or buffer into normalized dictionary of clinical metadata.
    Uses stop_before_pixels=True to parse headers without decompressing or allocating image pixels.
    Extracts SeriesInstanceUID for series grouping.
    """
    if isinstance(dicom_bytes_or_buffer, (bytes, bytearray)):
        buffer = io.BytesIO(dicom_bytes_or_buffer)
    else:
        buffer = dicom_bytes_or_buffer
        buffer.seek(0)

    try:
        ds = pydicom.dcmread(buffer, stop_before_pixels=True, force=True)
    except InvalidDicomError:
        raise ValueError("Invalid DICOM file or corrupted byte stream")

    # Extract tags safely
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

    cr_number = extract_cr_number(raw_pid, raw_pname, fallback_uid=study_uid or sop_uid, filepath=filepath)
    patient_name = clean_patient_name(raw_pname, fallback=filepath)
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
        'drive_file_id': drive_file_id,
        'file_name': fallback_name
    }


class GoogleDriveService:
    """Manages Google Drive API authentication, folder scanning, and streaming."""

    def __init__(self, root_dir=None):
        self.root_dir = root_dir or os.path.dirname(os.path.abspath(__file__))
        self.credentials_path = os.path.join(self.root_dir, 'credentials.json')
        self.token_path = os.path.join(self.root_dir, 'token.json')
        # Thread-local storage to guarantee each thread has its own isolated Google Drive client
        self._thread_local = threading.local()
        # Mutex lock to serialize token refresh and prevent file-write collisions
        self._auth_lock = threading.Lock()

    def is_configured(self):
        """Check if Google Drive credentials or cached tokens exist."""
        return os.path.exists(self.credentials_path) or os.path.exists(self.token_path)

    def get_credentials(self):
        """Thread-safe acquisition and refresh of Google Drive OAuth2 / Service Account credentials."""
        with self._auth_lock:
            creds = None
            # 1. Check for token.json (cached user credentials)
            if os.path.exists(self.token_path):
                try:
                    creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
                except Exception as e:
                    print(f"[Drive] Error loading token.json: {e}")
                    creds = None

            # 2. Check if credentials need refresh
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    with open(self.token_path, 'w', encoding='utf-8') as token_file:
                        token_file.write(creds.to_json())
                except Exception as e:
                    print(f"[Drive] Error refreshing token: {e}")
                    creds = None

            # 3. If no valid credentials, authenticate using credentials.json
            if not creds:
                if not os.path.exists(self.credentials_path):
                    raise FileNotFoundError(
                        f"Google Drive 'credentials.json' not found at {self.credentials_path}. "
                        "Please place your OAuth client ID or Service Account file there."
                    )

                try:
                    creds = service_account.Credentials.from_service_account_file(
                        self.credentials_path, scopes=SCOPES
                    )
                except Exception:
                    # Fallback to standard Installed App OAuth Flow
                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                    creds = flow.run_local_server(port=0)

                    with open(self.token_path, 'w', encoding='utf-8') as token_file:
                        token_file.write(creds.to_json())

            return creds

    def get_service(self):
        """Build or retrieve a thread-isolated Google Drive service object.
        Never shares an authorized HTTP client across threads to eliminate SSL state corruption ([SSL: WRONG_VERSION_NUMBER]).
        """
        if hasattr(self._thread_local, 'service') and self._thread_local.service is not None:
            return self._thread_local.service

        creds = self.get_credentials()
        # Create dedicated service instance for the calling thread with cache_discovery=False
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        self._thread_local.service = service
        return service

    def get_access_token(self):
        """Retrieve a valid Google Drive OAuth / Bearer access token string for client-side direct streaming."""
        try:
            if not self.is_configured():
                return None
            creds = self.get_credentials()
            if creds:
                if getattr(creds, 'expired', False) and getattr(creds, 'refresh_token', None):
                    creds.refresh(Request())
                elif not getattr(creds, 'valid', True):
                    creds.refresh(Request())
                return getattr(creds, 'token', None)
        except Exception as e:
            print(f"[Drive] Error obtaining access token: {e}")
            return None
        return None

    def find_file_id_by_filename(self, file_name, parent_folder_id=None):
        """Lightweight Google Drive API query to locate cloud file_id matching a given filename.
        Caches results in memory to minimize API calls during local disk crawls.
        """
        if not self.is_configured():
            return None

        if not hasattr(self, '_cloud_file_cache'):
            self._cloud_file_cache = {}

        cache_key = f"{parent_folder_id or ''}:{file_name}"
        if cache_key in self._cloud_file_cache:
            return self._cloud_file_cache[cache_key]

        try:
            service = self.get_service()
            safe_name = file_name.replace("'", "\\'")
            q = f"name = '{safe_name}' and trashed = false"
            if parent_folder_id:
                q += f" and '{parent_folder_id}' in parents"
            results = service.files().list(
                q=q,
                fields="files(id, name)",
                pageSize=1
            ).execute()
            files = results.get('files', [])
            if files:
                fid = files[0]['id']
                self._cloud_file_cache[cache_key] = fid
                return fid
            self._cloud_file_cache[cache_key] = None
        except Exception as e:
            print(f"[Drive Cross-Reference] Warning querying cloud file '{file_name}': {e}")
            self._cloud_file_cache[cache_key] = None

        return None

    def find_folder_id_by_name(self, folder_name, parent_folder_id=None):
        """Find the corresponding cloud folder_id for a directory name."""
        if not self.is_configured():
            return None

        if not hasattr(self, '_cloud_folder_cache'):
            self._cloud_folder_cache = {}

        cache_key = f"{parent_folder_id or ''}:{folder_name}"
        if cache_key in self._cloud_folder_cache:
            return self._cloud_folder_cache[cache_key]

        try:
            service = self.get_service()
            safe_name = folder_name.replace("'", "\\'")
            q = f"name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            if parent_folder_id:
                q += f" and '{parent_folder_id}' in parents"
            results = service.files().list(
                q=q,
                fields="files(id, name)",
                pageSize=1
            ).execute()
            files = results.get('files', [])
            if files:
                fid = files[0]['id']
                self._cloud_folder_cache[cache_key] = fid
                return fid
            self._cloud_folder_cache[cache_key] = None
        except Exception as e:
            print(f"[Drive Cross-Reference] Warning querying cloud folder '{folder_name}': {e}")
            self._cloud_folder_cache[cache_key] = None

        return None

    def get_subfolders(self, folder_id):
        """Retrieve all immediate subfolders located inside a given Google Drive folder."""
        service = self.get_service()
        subfolders = []
        page_token = None
        query = (
            f"'{folder_id}' in parents and trashed = false and "
            f"mimeType = 'application/vnd.google-apps.folder'"
        )
        while True:
            results = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=100,
                pageToken=page_token
            ).execute()
            subfolders.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        return subfolders

    def list_dicom_files_in_single_folder(self, folder_id):
        """Query a single Google Drive folder for files that appear to be DICOM or medical imaging."""
        service = self.get_service()
        dicom_files = []
        page_token = None
        query = (
            f"'{folder_id}' in parents and trashed = false and "
            f"mimeType != 'application/vnd.google-apps.folder' and "
            f"(name contains '.dcm' or name contains '.DCM' or "
            f"mimeType = 'application/dicom' or mimeType = 'application/octet-stream')"
        )
        while True:
            results = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageSize=100,
                pageToken=page_token
            ).execute()
            dicom_files.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        return dicom_files

    def list_dicom_files_recursive(self, root_folder_id):
        """Traverse the directory tree starting from root_folder_id to find all matching DICOM files in all subfolders."""
        folder_queue = [root_folder_id]
        visited_folders = set()
        all_dicom_files = []
        folders_scanned_count = 0

        while folder_queue:
            current_folder_id = folder_queue.pop(0)
            if current_folder_id in visited_folders:
                continue
            visited_folders.add(current_folder_id)
            folders_scanned_count += 1

            # 1. Discover all subfolders inside current_folder_id
            try:
                subfolders = self.get_subfolders(current_folder_id)
                for sf in subfolders:
                    if sf['id'] not in visited_folders:
                        folder_queue.append(sf['id'])
            except Exception as e:
                print(f"[Drive] Error querying subfolders for folder '{current_folder_id}': {e}")

            # 2. Discover all candidate DICOM files inside current_folder_id
            try:
                folder_files = self.list_dicom_files_in_single_folder(current_folder_id)
                all_dicom_files.extend(folder_files)
            except Exception as e:
                print(f"[Drive] Error querying DICOM files in folder '{current_folder_id}': {e}")

        return all_dicom_files, folders_scanned_count

    def list_dicom_files_in_folder(self, folder_id, recursive=True):
        """Query Google Drive folder for DICOM files (recursive by default)."""
        if recursive:
            files, _ = self.list_dicom_files_recursive(folder_id)
            return files
        return self.list_dicom_files_in_single_folder(folder_id)

    def download_file_to_memory(self, file_id):
        """Download a Google Drive file into an in-memory BytesIO buffer."""
        service = self.get_service()
        request = service.files().get_media(fileId=file_id)
        file_buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(file_buffer, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        file_buffer.seek(0)
        return file_buffer.getvalue()

    def sync_folder(self, folder_id, db_session, models, recursive=True, on_progress=None):
        """Recursively scan folder tree, download each DICOM header in memory without pixels
        (stop_before_pixels=True), populate SQLite immediately, and release memory buffers.
        Includes real-time terminal print() statements for every file processed.
        """
        print(f"\n[Drive Crawler] Beginning recursive scan in folder: {folder_id}...")
        if recursive:
            files, folders_scanned = self.list_dicom_files_recursive(folder_id)
        else:
            files = self.list_dicom_files_in_single_folder(folder_id)
            folders_scanned = 1

        total_files = len(files)
        print(f"[Drive Crawler] Traversal complete: {folders_scanned} folder(s) scanned, {total_files} candidate DICOM file(s) found.")

        ingested_count = 0
        skipped_count = 0
        errors = []

        Patient = models['Patient']
        Scan = models['Scan']

        for idx, f in enumerate(files, start=1):
            file_id = f['id']
            file_name = f['name']

            # Check if scan already exists in database
            existing_scan = db_session.query(Scan).filter_by(drive_file_id=file_id).first()
            if existing_scan:
                skipped_count += 1
                print(f"[Drive Crawler] [{idx}/{total_files}] Skipped existing scan: '{file_name}' (ID: {file_id})")
                if on_progress:
                    on_progress({'current': idx, 'total': total_files, 'ingested': ingested_count, 'skipped': skipped_count})
                continue

            print(f"[Drive Crawler] [{idx}/{total_files}] Downloading header for '{file_name}' (ID: {file_id})...")
            file_buffer = None
            try:
                # 1. Download file bytes into temporary memory buffer
                raw_bytes = self.download_file_to_memory(file_id)
                file_buffer = io.BytesIO(raw_bytes)
                del raw_bytes  # Immediate deallocation

                # 2. Extract clinical metadata without reading heavy pixel arrays
                meta = parse_dicom_bytes(file_buffer, fallback_name=file_name, drive_file_id=file_id)

                # 3. Upsert Patient record
                patient = db_session.query(Patient).filter_by(cr_number=meta['cr_number']).first()
                if not patient:
                    patient = Patient(
                        cr_number=meta['cr_number'],
                        patient_name=meta['patient_name'],
                        age=meta['age'],
                        gender=meta['gender']
                    )
                    db_session.add(patient)
                    db_session.flush()
                else:
                    if (not patient.patient_name or patient.patient_name in ('Anonymous', 'Unknown Patient', 'Unknown')) and meta['patient_name'] not in ('Anonymous', 'Unknown Patient', 'Unknown'):
                        patient.patient_name = meta['patient_name']
                    if (not patient.age or patient.age in ('Unknown', 'N/A')) and meta['age'] not in ('Unknown', 'N/A'):
                        patient.age = meta['age']
                    if (not patient.gender or patient.gender in ('Other', 'Unknown')) and meta['gender'] not in ('Other', 'Unknown'):
                        patient.gender = meta['gender']

                # 4. Grouping into Series (Scan) record
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
                    # File belongs to an existing series: register instance and increment count
                    existing_series.add_instance_file(file_id)
                    db_session.commit()
                    skipped_count += 1
                    print(f"[Drive Crawler] [{idx}/{total_files}] -> Grouped into Series #{existing_series.id} (Total Slices: {existing_series.instance_count})")
                else:
                    import json
                    scan = Scan(
                        cr_number=patient.cr_number,
                        drive_file_id=file_id,
                        file_name=file_name,
                        modality=meta['modality'],
                        date_of_test=meta['date_of_test'],
                        series_description=meta['series_description'],
                        study_instance_uid=meta['study_instance_uid'],
                        series_instance_uid=series_uid or f"SERIES_{file_id}",
                        sop_instance_uid=meta['sop_instance_uid'],
                        instance_count=1,
                        instance_files=json.dumps([file_id])
                    )
                    db_session.add(scan)
                    db_session.commit()
                    ingested_count += 1

                print(f"[Drive Crawler] [{idx}/{total_files}] -> PROCESSED: CR={patient.cr_number} | Patient='{patient.patient_name}' | Modality={meta['modality']} | Date={meta['date_of_test']}")

                if on_progress:
                    on_progress({
                        'current': idx,
                        'total': total_files,
                        'ingested': ingested_count,
                        'skipped': skipped_count,
                        'last_patient': f"{patient.cr_number} - {patient.patient_name}"
                    })

            except Exception as e:
                db_session.rollback()
                err_msg = f"Failed to process '{file_name}' ({file_id}): {str(e)}"
                errors.append(err_msg)
                print(f"[Drive Crawler] [{idx}/{total_files}] -> ERROR: {err_msg}")
            finally:
                # 5. Immediately clear and close the in-memory buffer to release RAM
                if file_buffer is not None:
                    file_buffer.close()
                    del file_buffer

        print(f"[Drive Crawler] Sync Complete: {ingested_count} newly ingested, {skipped_count} skipped, {len(errors)} error(s).\n")

        return {
            'total_found': total_files,
            'folders_scanned': folders_scanned,
            'ingested': ingested_count,
            'skipped': skipped_count,
            'errors': errors
        }
