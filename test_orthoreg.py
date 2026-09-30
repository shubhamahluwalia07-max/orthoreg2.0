import os
import io
import unittest
from app import app, db, PATIENT_SEMANTIC_FIELD_MAP, get_patient_attribute_for_field, is_identifying_patient_field, MODALITY_DISPLAY_NAMES, format_modality_name, generate_temp_password, ensure_patient_schema_migrations, run_retroactive_cr_migration
from models import User, Patient, Scan, Trial, CustomData, TrialPatientData
from drive_service import extract_cr_number, extract_strict_12digit_cr, clean_patient_name, clean_age, parse_dicom_bytes

TEST_CR_NUMBERS = [
    'CR990011', 'CR778899', 'CR334455', 'CR889900', 'CR888111',
    'CR777000', 'CR556677', 'CR_SERIES_TEST', 'CR_STREAM_TEST', 'CR999888',
    '202410158941', '202311223344', '201806126956', '201502060432',
    'CR_STRICT_TEST', 'CR_MIGRATE_OLD'
]
TEST_TRIALS = ['HIP-ARTHROPLASTY-2026', 'EXPORT-TRIAL', 'EDC-PREFILL-TRIAL', 'SCHEMA-MGMT-TRIAL']

class OrthoregTestCase(unittest.TestCase):

    def _cleanup_test_data(self):
        with app.app_context():
            try:
                for cr in TEST_CR_NUMBERS:
                    db.session.execute(db.text("DELETE FROM patient_trials WHERE patient_cr = :cr"), {"cr": cr})
                    db.session.execute(db.text("DELETE FROM trial_patient_data WHERE cr_number = :cr"), {"cr": cr})
                    db.session.execute(db.text("DELETE FROM custom_data WHERE cr_number = :cr"), {"cr": cr})
                    db.session.execute(db.text("DELETE FROM scans WHERE cr_number = :cr"), {"cr": cr})
                    db.session.execute(db.text("DELETE FROM patients WHERE cr_number = :cr"), {"cr": cr})
                for tname in TEST_TRIALS:
                    db.session.execute(db.text("DELETE FROM patient_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                    db.session.execute(db.text("DELETE FROM scan_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                    db.session.execute(db.text("DELETE FROM trial_patient_data WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                    db.session.execute(db.text("DELETE FROM trials WHERE trial_name = :tn"), {"tn": tname})
                db.session.commit()
            except Exception:
                db.session.rollback()

    def setUp(self):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        self.client = app.test_client()

        self._cleanup_test_data()

        with app.app_context():
            db.create_all()
            # Seed test users safely
            admin = User.query.filter_by(username='test_admin').first()
            if not admin:
                admin = User(username='test_admin', role='Admin')
                admin.set_password('pass123')
                db.session.add(admin)
            else:
                admin.set_password('pass123')

            viewer = User.query.filter_by(username='test_viewer').first()
            if not viewer:
                viewer = User(username='test_viewer', role='Viewer')
                viewer.set_password('pass123')
                db.session.add(viewer)
            else:
                viewer.set_password('pass123')

            db.session.commit()

    def tearDown(self):
        self._cleanup_test_data()
        with app.app_context():
            db.session.close()

    def test_regex_cr_extraction(self):
        """Test regex logic for extracting CR numbers from clinical metadata."""
        # Standard formats
        self.assertEqual(extract_cr_number('CR123456', ''), 'CR123456')
        self.assertEqual(extract_cr_number('CR-98765', ''), 'CR98765')
        self.assertEqual(extract_cr_number('CR 445566', ''), 'CR445566')
        self.assertEqual(extract_cr_number('UHID-112233', ''), 'UHID112233')
        # Embedded in patient name
        self.assertEqual(extract_cr_number('', 'KUMAR^RAJESH CR778899'), 'CR778899')
        # Pure numeric patient ID
        self.assertEqual(extract_cr_number('892019', 'PATEL^ANITA'), 'CR892019')

    def test_patient_name_cleaning(self):
        """Test cleaning DICOM name format."""
        self.assertEqual(clean_patient_name('DOE^JOHN'), 'JOHN DOE')
        self.assertEqual(clean_patient_name('SINGH^HARPREET^DR'), 'HARPREET DR SINGH')
        self.assertEqual(clean_patient_name('ANONYMOUS'), 'ANONYMOUS')

    def test_user_authentication_and_rbac(self):
        """Test authentication and role separation."""
        with app.app_context():
            admin = User.query.filter_by(username='test_admin').first()
            viewer = User.query.filter_by(username='test_viewer').first()
            self.assertTrue(admin.check_password('pass123'))
            self.assertTrue(admin.is_admin_or_pi)
            self.assertFalse(viewer.is_admin_or_pi)

    def test_login_flow_and_dashboard_access(self):
        """Test login route and redirection."""
        # Unauthenticated request redirects to login
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers['Location'])

        # Successful login as admin
        login_res = self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'}, follow_redirects=True)
        self.assertEqual(login_res.status_code, 200)
        self.assertIn(b'ORTHOREG', login_res.data)
        self.assertIn(b'Admin', login_res.data)

    def test_patient_and_scan_lifecycle(self):
        """Test creating a patient, adding a scan, custom data, and assigning a trial."""
        with app.app_context():
            patient = Patient(
                cr_number='CR990011',
                patient_name='Test Patient',
                age='50 Yrs',
                gender='M'
            )
            trial = Trial(trial_name='HIP-ARTHROPLASTY-2026', description='Test Trial Protocol')
            patient.trials.append(trial)

            scan = Scan(
                cr_number=patient.cr_number,
                drive_file_id='test_drive_id_123',
                file_name='hip_ap.dcm',
                modality='DX',
                date_of_test='2026-09-20'
            )

            custom_col = CustomData(
                cr_number=patient.cr_number,
                field_name='Harris Hip Score',
                field_type='text',
                field_value='88 / 100'
            )

            db.session.add_all([patient, trial, scan, custom_col])
            db.session.commit()

            # Verify query
            p = Patient.query.filter_by(cr_number='CR990011').first()
            self.assertIsNotNone(p)
            self.assertEqual(len(p.scans), 1)
            self.assertEqual(len(p.trials), 1)
            self.assertEqual(len(p.custom_data), 1)
            self.assertEqual(p.custom_data[0].field_name, 'Harris Hip Score')

    def test_export_cohort_csv(self):
        """Test pandas CSV cohort export."""
        # Login first
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})

        with app.app_context():
            trial = Trial(trial_name='EXPORT-TRIAL', description='Testing Export')
            p = Patient(cr_number='CR777000', patient_name='Export Patient', age='30 Yrs', gender='F')
            p.trials.append(trial)
            db.session.add_all([trial, p])
            db.session.commit()
            trial_id = trial.id

        res = self.client.get(f'/trials/{trial_id}/export')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, 'text/csv')
        self.assertIn(b'CR777000', res.data)
        self.assertIn(b'Export Patient', res.data)

    def test_dicom_viewer_route(self):
        """Test the viewer page and DICOM streaming route."""
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})

        with app.app_context():
            p = Patient(cr_number='CR888111', patient_name='DICOM Patient', age='40 Yrs', gender='M')
            db.session.add(p)
            db.session.commit()

            # Create dummy file
            sample_path = os.path.join(app.root_path, 'sample_dicoms', 'test_stream.dcm')
            os.makedirs(os.path.dirname(sample_path), exist_ok=True)
            with open(sample_path, 'wb') as f:
                f.write(b'\x00' * 128 + b'DICM' + b'\x00' * 512)

            scan = Scan(
                cr_number=p.cr_number,
                drive_file_id='local_test_stream.dcm',
                file_name='test_stream.dcm',
                modality='CR',
                local_storage_path=sample_path
            )
            db.session.add(scan)
            db.session.commit()
            scan_id = scan.id

        # Test viewer page
        view_res = self.client.get(f'/viewer/{scan_id}')
        self.assertEqual(view_res.status_code, 200)
        self.assertIn(b'DICOM PACS Viewer', view_res.data)
        self.assertIn(b'Bone', view_res.data)

        # Test streaming route
        stream_res = self.client.get(f'/scan/{scan_id}/dicom')
        self.assertEqual(stream_res.status_code, 200)
        self.assertEqual(stream_res.mimetype, 'application/dicom')
        self.assertTrue(stream_res.data.startswith(b'\x00' * 128 + b'DICM'))
        stream_res.close()

    def test_recursive_drive_search(self):
        """Test recursive Google Drive subfolder discovery and traversal."""
        from drive_service import GoogleDriveService
        from unittest.mock import MagicMock

        drive = GoogleDriveService(root_dir=app.root_path)

        # Mock folder tree:
        # root_folder
        #   ├── patient_subfolder_1
        #   │     ├── series_A (contains file1.dcm, file2.dcm)
        #   └── patient_subfolder_2 (contains file3.dcm)
        subfolders_map = {
            'root_folder': [{'id': 'sub_1', 'name': 'Patient_01'}, {'id': 'sub_2', 'name': 'Patient_02'}],
            'sub_1': [{'id': 'series_A', 'name': 'Series_AP'}],
            'sub_2': [],
            'series_A': []
        }
        files_map = {
            'root_folder': [],
            'sub_1': [],
            'sub_2': [{'id': 'f3', 'name': 'knee_lat.dcm', 'mimeType': 'application/dicom'}],
            'series_A': [
                {'id': 'f1', 'name': 'femur_ap.dcm', 'mimeType': 'application/dicom'},
                {'id': 'f2', 'name': 'femur_lat.dcm', 'mimeType': 'application/dicom'}
            ]
        }

        drive.get_subfolders = MagicMock(side_effect=lambda fid: subfolders_map.get(fid, []))
        drive.list_dicom_files_in_single_folder = MagicMock(side_effect=lambda fid: files_map.get(fid, []))

        files, folders_scanned = drive.list_dicom_files_recursive('root_folder')
        self.assertEqual(folders_scanned, 4)  # root_folder + sub_1 + sub_2 + series_A
        self.assertEqual(len(files), 3)       # f1, f2, f3
        file_ids = [f['id'] for f in files]
        self.assertIn('f1', file_ids)
        self.assertIn('f2', file_ids)
        self.assertIn('f3', file_ids)

    def test_thread_isolation_drive_service(self):
        """Test that GoogleDriveService isolates service objects across threads using thread-local storage."""
        import threading
        from drive_service import GoogleDriveService, DEFAULT_DRIVE_FOLDER_ID
        from unittest.mock import MagicMock

        self.assertEqual(DEFAULT_DRIVE_FOLDER_ID, "1eEnoJi0hYHgrPmtWJub0fA6W_oPOL2iR")

        drive = GoogleDriveService(root_dir=app.root_path)
        drive.get_credentials = MagicMock(return_value=MagicMock())

        thread_services = {}

        def worker(thread_name):
            # In each thread, getting service should create and store a separate instance in _thread_local
            s = drive.get_service()
            thread_services[thread_name] = id(s)

        t1 = threading.Thread(target=worker, args=('thread_1',))
        t2 = threading.Thread(target=worker, args=('thread_2',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both threads got a service, and their memory IDs are distinct (different objects!)
        self.assertIn('thread_1', thread_services)
        self.assertIn('thread_2', thread_services)
        self.assertNotEqual(thread_services['thread_1'], thread_services['thread_2'])

    def test_api_crawler_status(self):
        """Test the live crawler telemetry API endpoint."""
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
        res = self.client.get('/api/crawler-status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('crawler', data)
        self.assertIn('total_patients', data)
        self.assertIn('total_scans', data)
        self.assertIn('is_running', data['crawler'])
        self.assertIn('folders_scanned', data)
        self.assertIn('files_scanned', data)
        self.assertIn('ingested', data)
        self.assertIn('skipped', data)
        self.assertIn('folders_scanned', data['crawler'])
        self.assertIn('files_scanned', data['crawler'])
        self.assertIn('ingested', data['crawler'])
        self.assertIn('skipped', data['crawler'])

    def test_crawler_banner_text_and_dynamic_progress(self):
        """Test that the dashboard crawler banner references local repository and dynamic files scanned."""
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Indexing Local Radiology Repository", html)
        self.assertIn("Files Scanned:", html)
        self.assertNotIn("Scanning Google Drive repository...", html)
        self.assertNotIn("Progress: <strong id=\"crawlerProgress\" class=\"text-white\">0/0</strong>", html)

    def test_local_drive_path_configuration(self):
        """Test that LOCAL_DRIVE_PATH is configured at the top of app.py."""
        from app import LOCAL_DRIVE_PATH
        self.assertEqual(LOCAL_DRIVE_PATH, r"D:\RADIOLOGY DATA")

    def test_is_dicom_file_helper(self):
        """Test DICOM identification by extension and preamble."""
        from local_scanner import is_dicom_file
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Non-DICOM extensions
            txt_file = os.path.join(tmpdir, "notes.txt")
            with open(txt_file, "w") as f:
                f.write("text content")
            self.assertFalse(is_dicom_file(txt_file))

            # 2. .dcm extension
            dcm_file = os.path.join(tmpdir, "image.dcm")
            with open(dcm_file, "wb") as f:
                f.write(b"data")
            self.assertTrue(is_dicom_file(dcm_file))

            # 3. Preamble check (no extension)
            raw_dicm = os.path.join(tmpdir, "10002938")
            with open(raw_dicm, "wb") as f:
                f.write(b'\x00' * 128 + b'DICM' + b'\x00' * 50)
            self.assertTrue(is_dicom_file(raw_dicm))

    def test_extract_metadata_from_file_and_local_streaming(self):
        """Test fast metadata extraction and local streaming via send_file."""
        from local_scanner import extract_metadata_from_file
        from sample_data import create_synthetic_dicom
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            dcm_path = os.path.join(tmpdir, "test_extract.dcm")
            create_synthetic_dicom(
                filepath=dcm_path,
                patient_name="TEST^PATIENT CR556677",
                cr_number="CR556677",
                modality="DX",
                age="045Y",
                sex="F",
                study_date="20260924",
                series_desc="Digital Radiograph Knee AP"
            )

            # Test extract_metadata_from_file
            meta = extract_metadata_from_file(dcm_path)
            self.assertEqual(meta['cr_number'], "CR556677")
            self.assertIn("PATIENT", meta['patient_name'])
            self.assertEqual(meta['modality'], "DX")
            self.assertEqual(meta['gender'], "F")
            self.assertEqual(meta['local_file_path'], os.path.abspath(dcm_path))
            self.assertGreater(meta['file_size_bytes'], 0)

            # Test DB persistence with local_file_path and streaming route
            self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
            with app.app_context():
                p = Patient(cr_number=meta['cr_number'], patient_name=meta['patient_name'], age=meta['age'], gender=meta['gender'])
                s = Scan(
                    cr_number=p.cr_number,
                    local_file_path=meta['local_file_path'],
                    file_name=meta['file_name'],
                    modality=meta['modality'],
                    date_of_test=meta['date_of_test'],
                    file_size_bytes=meta['file_size_bytes']
                )
                db.session.add_all([p, s])
                db.session.commit()
                scan_id = s.id

            stream_res = self.client.get(f'/scan/{scan_id}/dicom')
            self.assertEqual(stream_res.status_code, 200)
            self.assertEqual(stream_res.mimetype, 'application/dicom')
            self.assertIn('inline', stream_res.headers.get('Content-Disposition', ''))
            stream_res.close()

    def test_scan_local_directory_with_pruning(self):
        """Test recursive scan_local_directory, checking that .tmp.driveupload is ignored."""
        from local_scanner import scan_local_directory
        from sample_data import create_synthetic_dicom
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            # Create nested patient subfolder
            patient_dir = os.path.join(tmpdir, "CT", "PATIENT_A 202601")
            os.makedirs(patient_dir, exist_ok=True)
            dcm_file = os.path.join(patient_dir, "IMG-0001.dcm")
            create_synthetic_dicom(
                filepath=dcm_file,
                patient_name="PATIENT^ALICE CR334455",
                cr_number="CR334455",
                modality="CT",
                age="060Y",
                sex="F",
                study_date="20260920",
                series_desc="Axial CT Pelvis"
            )

            # Create ignored .tmp.driveupload directory
            tmp_upload_dir = os.path.join(tmpdir, ".tmp.driveupload")
            os.makedirs(tmp_upload_dir, exist_ok=True)
            with open(os.path.join(tmp_upload_dir, "uploading.tmp"), "w") as f:
                f.write("in-progress drive sync")

            with app.app_context():
                models = {'Patient': Patient, 'Scan': Scan}
                result = scan_local_directory(tmpdir, db.session, models)
                self.assertEqual(result['total_found'], 1)
                self.assertEqual(result['ingested'], 1)

                # Verify in database
                scan = Scan.query.filter_by(cr_number="CR334455").first()
                self.assertIsNotNone(scan)
                self.assertEqual(scan.local_file_path, os.path.abspath(dcm_file))
                self.assertEqual(scan.modality, "CT")

    def test_scan_local_directory_dynamic_progress_reporting(self):
        """Test continuous dynamic progress tracking with on_progress callback."""
        from local_scanner import scan_local_directory
        from sample_data import create_synthetic_dicom
        import tempfile

        progress_reports = []

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            patient_dir = os.path.join(tmpdir, "MRI", "PATIENT_B")
            os.makedirs(patient_dir, exist_ok=True)
            dcm_file = os.path.join(patient_dir, "IMG-0002.dcm")
            create_synthetic_dicom(
                filepath=dcm_file,
                patient_name="PATIENT^BOB CR778899",
                cr_number="CR778899",
                modality="MR",
                age="045Y",
                sex="M",
                study_date="20260921",
                series_desc="Sagittal Knee MRI"
            )

            with app.app_context():
                models = {'Patient': Patient, 'Scan': Scan}
                result = scan_local_directory(
                    tmpdir, db.session, models,
                    on_progress=lambda p: progress_reports.append(dict(p))
                )
                self.assertEqual(result['ingested'], 1)
                self.assertGreater(len(progress_reports), 0)
                last_p = progress_reports[-1]
                self.assertIn('folders_scanned', last_p)
                self.assertIn('files_scanned', last_p)
                self.assertIn('ingested', last_p)
                self.assertIn('skipped', last_p)
                self.assertEqual(last_p['ingested'], 1)

    def test_series_grouping_multiple_slices_single_scan_record(self):
        """Test that multiple DICOM slices sharing the same SeriesInstanceUID are grouped into a single Scan record."""
        from local_scanner import scan_local_directory
        from sample_data import create_synthetic_dicom
        import tempfile
        import pydicom

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            shared_series_uid = "1.2.840.10008.5.1.4.1.1.2.99999.1"
            f1 = os.path.join(tmpdir, "slice_001.dcm")
            f2 = os.path.join(tmpdir, "slice_002.dcm")

            create_synthetic_dicom(
                filepath=f1,
                patient_name="SERIES^PATIENT CR889900",
                cr_number="CR889900",
                modality="CT",
                age="052Y",
                sex="M",
                study_date="20260925",
                series_desc="CT Bone Pelvis Axial"
            )
            # Override SeriesInstanceUID to guarantee exact match
            ds1 = pydicom.dcmread(f1)
            ds1.SeriesInstanceUID = shared_series_uid
            ds1.InstanceNumber = 1
            ds1.save_as(f1)

            create_synthetic_dicom(
                filepath=f2,
                patient_name="SERIES^PATIENT CR889900",
                cr_number="CR889900",
                modality="CT",
                age="052Y",
                sex="M",
                study_date="20260925",
                series_desc="CT Bone Pelvis Axial"
            )
            ds2 = pydicom.dcmread(f2)
            ds2.SeriesInstanceUID = shared_series_uid
            ds2.InstanceNumber = 2
            ds2.save_as(f2)

            with app.app_context():
                models = {'Patient': Patient, 'Scan': Scan}
                res = scan_local_directory(tmpdir, db.session, models)
                self.assertEqual(res['total_found'], 2)
                self.assertEqual(res['ingested'], 1)  # Only 1 Series record ingested!
                self.assertEqual(res['skipped'], 1)   # Second slice grouped into the existing series

                # Verify single Scan record exists in database
                scans = Scan.query.filter_by(cr_number="CR889900").all()
                self.assertEqual(len(scans), 1)
                series_record = scans[0]
                self.assertEqual(series_record.series_instance_uid, shared_series_uid)
                self.assertEqual(series_record.instance_count, 2)
                self.assertEqual(len(series_record.get_instance_files()), 2)
                self.assertEqual(series_record.modality, "CT")

                # Verify patient demographics
                patient = Patient.query.filter_by(cr_number="CR889900").first()
                self.assertIsNotNone(patient)
                self.assertEqual(patient.patient_name, "PATIENT SERIES")
                self.assertEqual(patient.age, "52 Yrs")
                self.assertEqual(patient.gender, "M")

    def test_api_patient_series_endpoint(self):
        """Test /api/patient/<cr_number>/series endpoint returns grouped series list."""
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
        with app.app_context():
            p = Patient(cr_number="CR_SERIES_TEST", patient_name="Dr Series Test", age="60 Yrs", gender="F")
            s1 = Scan(
                cr_number=p.cr_number,
                modality="CT",
                date_of_test="2026-09-25",
                series_description="CT Pelvis 3D",
                series_instance_uid="UID.111",
                instance_count=24
            )
            s2 = Scan(
                cr_number=p.cr_number,
                modality="DX",
                date_of_test="2026-09-20",
                series_description="Knee AP",
                series_instance_uid="UID.222",
                instance_count=1
            )
            db.session.add_all([p, s1, s2])
            db.session.commit()

        res = self.client.get('/api/patient/CR_SERIES_TEST/series')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['cr_number'], "CR_SERIES_TEST")
        self.assertEqual(len(data['series']), 2)
        self.assertEqual(data['series'][0]['modality'], "CT")
        self.assertEqual(data['series'][0]['instance_count'], 24)
        self.assertIn('/viewer/', data['series'][0]['viewer_url'])

    def test_dashboard_ui_view_radiology_data_button_and_modal(self):
        """Test that the dashboard table renders the 'View Radiology Data' button and popup modal."""
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Check button label
        self.assertIn("View Radiology Data", html)
        # Check popup modal structure
        self.assertIn('id="radiologyModal"', html)
        self.assertIn('id="modalSeriesList"', html)
        self.assertIn('openRadiologyModal', html)

    def test_clean_demographics_no_blank_rows(self):
        """Test that empty or malformed clinical tags never produce blank UI fields."""
        self.assertEqual(clean_patient_name(""), "Anonymous")
        self.assertEqual(clean_patient_name(None), "Anonymous")
        self.assertEqual(clean_patient_name("   "), "Anonymous")
        self.assertEqual(clean_patient_name("DOE^JOHN"), "JOHN DOE")
        self.assertEqual(clean_age(""), "Unknown")
        self.assertEqual(clean_age(None), "Unknown")
        self.assertEqual(clean_age("035Y"), "35 Yrs")

    def test_stream_dicom_success_and_send_file(self):
        """Test streaming raw DICOM bytes returns 200 with application/dicom mimetype."""
        from sample_data import create_synthetic_dicom
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp_dcm:
            tmp_path = tmp_dcm.name

        try:
            create_synthetic_dicom(
                filepath=tmp_path,
                patient_name="STREAM^PATIENT CR_STREAM_TEST",
                cr_number="CR_STREAM_TEST",
                modality="DX",
                age="040Y",
                sex="F",
                study_date="20260925",
                series_desc="Digital Radiograph Knee AP"
            )

            with app.app_context():
                p = Patient(cr_number="CR_STREAM_TEST", patient_name="STREAM PATIENT", age="40 Yrs", gender="F")
                s = Scan(
                    cr_number=p.cr_number,
                    local_file_path=tmp_path,
                    file_name="stream_test.dcm",
                    modality="DX"
                )
                db.session.add_all([p, s])
                db.session.commit()
                scan_id = s.id

            # Log in
            self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})

            # 1. Test canonical route /scan/<id>/dicom
            res1 = self.client.get(f'/scan/{scan_id}/dicom')
            self.assertEqual(res1.status_code, 200)
            self.assertIn('application/dicom', res1.headers.get('Content-Type', ''))
            self.assertGreater(len(res1.data), 128)

            # 2. Test alias route /api/dicom/<id>
            res2 = self.client.get(f'/api/dicom/{scan_id}')
            self.assertEqual(res2.status_code, 200)
            self.assertIn('application/dicom', res2.headers.get('Content-Type', ''))
            self.assertEqual(res1.data, res2.data)

            res1.close()
            res2.close()

        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def test_stream_dicom_missing_file_logs_and_returns_404_text(self):
        """Test that a missing DICOM file returns 404 text/plain without crashing or sending HTML."""
        with app.app_context():
            p = Patient(cr_number="CR_STREAM_TEST", patient_name="MISSING FILE", age="40 Yrs", gender="M")
            s = Scan(
                cr_number=p.cr_number,
                local_file_path=r"D:\non_existent_path\fake_slice.dcm",
                file_name="fake_slice.dcm",
                modality="CT"
            )
            db.session.add_all([p, s])
            db.session.commit()
            scan_id = s.id

        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})
        res = self.client.get(f'/api/dicom/{scan_id}')
        self.assertEqual(res.status_code, 404)
        self.assertIn('text/plain', res.headers.get('Content-Type', ''))
        self.assertIn('DICOM Stream Error', res.get_data(as_text=True))

    def test_stream_dicom_unauthorized_returns_401(self):
        """Test that unauthenticated requests to /api/dicom/ return 401 instead of HTML login redirect."""
        # Ensure client is logged out
        self.client.get('/logout')
        res = self.client.get('/api/dicom/999999')
        self.assertEqual(res.status_code, 401)
        self.assertIn('text/plain', res.headers.get('Content-Type', ''))
        self.assertIn('Authentication required', res.get_data(as_text=True))

    def test_cornerstone_webworker_and_codecs_served_locally(self):
        """Test that cornerstoneWADOImageLoaderWebWorker.js and Codecs are present and served by Flask."""
        res_worker = self.client.get('/static/js/cornerstoneWADOImageLoaderWebWorker.js')
        self.assertEqual(res_worker.status_code, 200)
        self.assertIn('javascript', res_worker.headers.get('Content-Type', ''))
        self.assertGreater(len(res_worker.data), 100000)

        res_codecs = self.client.get('/static/js/cornerstoneWADOImageLoaderCodecs.js')
        self.assertEqual(res_codecs.status_code, 200)
        self.assertIn('javascript', res_codecs.headers.get('Content-Type', ''))
        self.assertGreater(len(res_codecs.data), 100000)

        res_codecs.close()
        res_worker.close()


class DynamicRBACAndUserManagementTestCase(unittest.TestCase):
    """Test suite for Dynamic Role-Based Access Control and Admin User Management."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        self.client = app.test_client()

        with app.app_context():
            db.create_all()
            # Clean up all test users, patients, and trials
            for cr in TEST_CR_NUMBERS:
                db.session.execute(db.text("DELETE FROM patient_trials WHERE patient_cr = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM trial_patient_data WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM custom_data WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM scans WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM patients WHERE cr_number = :cr"), {"cr": cr})
            for tname in TEST_TRIALS:
                db.session.execute(db.text("DELETE FROM patient_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM scan_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM trial_patient_data WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM trials WHERE trial_name = :tn"), {"tn": tname})
            db.session.commit()

            for ident in [
                'rbac_admin@test.org', 'rbac_editor@test.org', 'rbac_viewer@test.org', 
                'request_user@test.org', 'invited_user@test.org',
                'invited_temp_user', 'setup_test_user', 'active_changer',
                'invited_temp@hospital.org', 'setup_test@hospital.org', 'active_changer@hospital.org'
            ]:
                u = User.query.filter((User.email == ident) | (User.username == ident)).first()
                if u:
                    db.session.delete(u)
            db.session.commit()

            # Seed Admin
            admin = User(email='rbac_admin@test.org', username='rbac_admin@test.org', name='RBAC Admin', role='Admin', status='Active')
            admin.set_password('AdminPass123!')
            db.session.add(admin)

            # Seed Editor
            editor = User(email='rbac_editor@test.org', username='rbac_editor@test.org', name='RBAC Editor', role='Editor', status='Active')
            editor.set_password('EditorPass123!')
            db.session.add(editor)

            # Seed User (Viewer)
            viewer = User(email='rbac_viewer@test.org', username='rbac_viewer@test.org', name='RBAC Viewer', role='User', status='Active')
            viewer.set_password('ViewerPass123!')
            db.session.add(viewer)

            db.session.commit()

    def tearDown(self):
        with app.app_context():
            for cr in TEST_CR_NUMBERS:
                db.session.execute(db.text("DELETE FROM patient_trials WHERE patient_cr = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM trial_patient_data WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM custom_data WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM scans WHERE cr_number = :cr"), {"cr": cr})
                db.session.execute(db.text("DELETE FROM patients WHERE cr_number = :cr"), {"cr": cr})
            for tname in TEST_TRIALS:
                db.session.execute(db.text("DELETE FROM patient_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM scan_trials WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM trial_patient_data WHERE trial_id IN (SELECT id FROM trials WHERE trial_name = :tn)"), {"tn": tname})
                db.session.execute(db.text("DELETE FROM trials WHERE trial_name = :tn"), {"tn": tname})
            db.session.commit()

            for ident in [
                'rbac_admin@test.org', 'rbac_editor@test.org', 'rbac_viewer@test.org', 
                'request_user@test.org', 'invited_user@test.org',
                'invited_temp_user', 'setup_test_user', 'active_changer',
                'invited_temp@hospital.org', 'setup_test@hospital.org', 'active_changer@hospital.org'
            ]:
                u = User.query.filter((User.email == ident) | (User.username == ident)).first()
                if u:
                    db.session.delete(u)
            db.session.commit()
            db.session.close()

    def test_access_request_and_pending_login_block(self):
        """External users can request access, creating a Pending account that cannot log in until approved."""
        # 1. Submit access request
        res = self.client.post('/request-access', data={
            'name': 'Dr. Access Requester',
            'email': 'request_user@test.org',
            'password': 'NewPassword123!',
            'confirm_password': 'NewPassword123!'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            user = User.query.filter_by(email='request_user@test.org').first()
            self.assertIsNotNone(user)
            self.assertEqual(user.status, 'Pending')
            self.assertEqual(user.role, 'User')
            self.assertFalse(user.is_active)

        # 2. Attempt to login while still pending
        login_res = self.client.post('/login', data={
            'username': 'request_user@test.org',
            'password': 'NewPassword123!'
        }, follow_redirects=True)
        self.assertEqual(login_res.status_code, 200)
        self.assertIn('pending administrative approval', login_res.get_data(as_text=True).lower())

    def test_admin_approves_pending_user_and_assigns_role(self):
        """Admin can view pending requests and approve with a specified role."""
        with app.app_context():
            u = User(email='request_user@test.org', username='request_user@test.org', name='Pending Researcher', role='User', status='Pending')
            u.set_password('ApprovedPass123!')
            db.session.add(u)
            db.session.commit()
            user_id = u.id

        # Non-admin cannot approve
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})
        res_fail = self.client.post(f'/admin/users/{user_id}/approve', data={'role': 'Editor'})
        self.assertEqual(res_fail.status_code, 403)
        self.client.get('/logout')

        # Admin logs in and approves as Editor
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})
        res_approve = self.client.post(f'/admin/users/{user_id}/approve', data={'role': 'Editor'}, follow_redirects=True)
        self.assertEqual(res_approve.status_code, 200)
        self.client.get('/logout')

        # Verify user is now Active and Editor
        with app.app_context():
            user = db.session.get(User, user_id)
            self.assertEqual(user.status, 'Active')
            self.assertEqual(user.role, 'Editor')
            self.assertTrue(user.is_active)

        # Now the approved user can log in successfully
        res_login = self.client.post('/login', data={'username': 'request_user@test.org', 'password': 'ApprovedPass123!'}, follow_redirects=True)
        self.assertEqual(res_login.status_code, 200)
        self.assertIn('dashboard', res_login.get_data(as_text=True).lower())

    def test_admin_invite_user(self):
        """Admin can directly invite a user with an assigned role."""
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})
        res = self.client.post('/admin/users/invite', data={
            'email': 'invited_user@test.org',
            'name': 'Invited Clinician',
            'role': 'Editor',
            'initial_password': 'InvitePassword123!'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            invited = User.query.filter_by(email='invited_user@test.org').first()
            self.assertIsNotNone(invited)
            self.assertEqual(invited.status, 'Active')
            self.assertEqual(invited.role, 'Editor')
            self.assertTrue(invited.check_password('InvitePassword123!'))
            self.assertTrue(invited.must_change_password)

    def test_role_hierarchy_and_permissions(self):
        """Verify Admin > Editor > User permission enforcement."""
        # 1. User (Viewer) cannot access Admin panel or perform mutations
        self.client.post('/login', data={'username': 'rbac_viewer@test.org', 'password': 'ViewerPass123!'})

        # Forbidden admin panel
        res_admin = self.client.get('/admin/users')
        self.assertEqual(res_admin.status_code, 403)

        # Forbidden patient edit
        res_edit = self.client.post('/patient/CR123456/edit', data={'patient_name': 'HACKED'})
        self.assertEqual(res_edit.status_code, 403)

        # Forbidden patient delete
        res_del = self.client.post('/patient/CR123456/delete')
        self.assertEqual(res_del.status_code, 403)

        # Forbidden trial create
        res_trial = self.client.post('/trials/create', data={'trial_name': 'FORBIDDEN'})
        self.assertEqual(res_trial.status_code, 403)

        # Allowed dashboard read
        res_dash = self.client.get('/dashboard')
        self.assertEqual(res_dash.status_code, 200)

        self.client.get('/logout')

        # 2. Editor can access dashboard, but cannot access admin panel or delete data
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})

        res_editor_panel = self.client.get('/admin/users')
        self.assertEqual(res_editor_panel.status_code, 403)

        res_editor_delete = self.client.post('/patient/CR123456/delete')
        self.assertEqual(res_editor_delete.status_code, 403)

        res_editor_create_trial = self.client.post('/trials/create', data={'trial_name': 'FORBIDDEN'})
        self.assertEqual(res_editor_create_trial.status_code, 403)

        self.client.get('/logout')

    def test_admin_revoke_and_safeguard(self):
        """Admin can revoke user access, but cannot revoke own account."""
        with app.app_context():
            admin = User.query.filter_by(email='rbac_admin@test.org').first()
            editor = User.query.filter_by(email='rbac_editor@test.org').first()
            admin_id = admin.id
            editor_id = editor.id

        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})

        # Cannot revoke own account
        res_self = self.client.post(f'/admin/users/{admin_id}/revoke', follow_redirects=True)
        self.assertEqual(res_self.status_code, 200)
        self.assertIn('cannot revoke your own access', res_self.get_data(as_text=True).lower())

        # Can revoke editor
        res_rev = self.client.post(f'/admin/users/{editor_id}/revoke', follow_redirects=True)
        self.assertEqual(res_rev.status_code, 200)

        with app.app_context():
            revoked_editor = db.session.get(User, editor_id)
            self.assertEqual(revoked_editor.status, 'Revoked')
            self.assertFalse(revoked_editor.is_active)

        self.client.get('/logout')

        # Revoked editor cannot log in
        res_editor_login = self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'}, follow_redirects=True)
        self.assertEqual(res_editor_login.status_code, 200)
        self.assertIn('access has been revoked', res_editor_login.get_data(as_text=True).lower())

    def test_semantic_field_mapping(self):
        """Test case-insensitive semantic field mapping to Patient model attributes."""
        # 1. Verify dictionary mapping
        self.assertIn('cr_no', PATIENT_SEMANTIC_FIELD_MAP)
        self.assertEqual(PATIENT_SEMANTIC_FIELD_MAP['cr_no'], 'cr_number')
        self.assertEqual(PATIENT_SEMANTIC_FIELD_MAP['name'], 'name')
        self.assertEqual(PATIENT_SEMANTIC_FIELD_MAP['age'], 'age')
        self.assertEqual(PATIENT_SEMANTIC_FIELD_MAP['gender'], 'gender')
        self.assertEqual(PATIENT_SEMANTIC_FIELD_MAP['sex'], 'gender')

        # 2. Case-insensitivity & normalization in mapping function
        test_patient = Patient(
            cr_number='CR999888',
            patient_name='Jane Clinical Doe',
            age='42 Yrs',
            gender='Female'
        )

        # CR Number variations
        for cr_field in ['cr_no', 'CR_NO', 'CR Number', 'cr_number', 'patient_cr', 'Cr No.']:
            self.assertEqual(get_patient_attribute_for_field(cr_field), 'cr_number')
            self.assertEqual(get_patient_attribute_for_field(cr_field, test_patient), 'CR999888')
            self.assertTrue(is_identifying_patient_field(cr_field))

        # Name variations
        for name_field in ['name', 'NAME', 'patient_name', 'Patient Name', 'PT_NAME', 'Full Name']:
            self.assertEqual(get_patient_attribute_for_field(name_field), 'name')
            self.assertEqual(get_patient_attribute_for_field(name_field, test_patient), 'Jane Clinical Doe')
            self.assertFalse(is_identifying_patient_field(name_field))

        # Age variations
        for age_field in ['age', 'AGE', 'patient_age', 'Patient Age']:
            self.assertEqual(get_patient_attribute_for_field(age_field), 'age')
            self.assertEqual(get_patient_attribute_for_field(age_field, test_patient), '42 Yrs')
            # Extract digits when field_type is Number
            self.assertEqual(get_patient_attribute_for_field(age_field, test_patient, field_type='Number'), '42')
            self.assertFalse(is_identifying_patient_field(age_field))

        # Gender / Sex variations
        for gender_field in ['gender', 'Gender', 'sex', 'SEX', 'patient_sex', 'Patient Gender']:
            self.assertEqual(get_patient_attribute_for_field(gender_field), 'gender')
            self.assertEqual(get_patient_attribute_for_field(gender_field, test_patient), 'Female')
            self.assertFalse(is_identifying_patient_field(gender_field))

        # Unrelated fields return None
        self.assertIsNone(get_patient_attribute_for_field('unrelated_score', test_patient))
        self.assertFalse(is_identifying_patient_field('unrelated_score'))

    def test_edc_demographic_prefill_and_readonly_lock(self):
        """Test trial dashboard EDC form pre-fills demographic attributes and sets readonly for CR Number."""
        with app.app_context():
            p = Patient.query.filter_by(cr_number='CR999888').first()
            if not p:
                p = Patient(
                    cr_number='CR999888',
                    patient_name='Prefill Test Patient',
                    age='55 Yrs',
                    gender='Male'
                )
                db.session.add(p)
            else:
                p.patient_name = 'Prefill Test Patient'
                p.age = '55 Yrs'
                p.gender = 'Male'

            trial = Trial.query.filter_by(trial_name='EDC-PREFILL-TRIAL').first()
            if not trial:
                trial = Trial(
                    trial_name='EDC-PREFILL-TRIAL',
                    description='Testing Dynamic EDC Prefill and Locking',
                    target_sample_size=10
                )
                db.session.add(trial)
            trial.set_data_schema([
                {'name': 'CR_No', 'type': 'Text'},
                {'name': 'Patient Name', 'type': 'Text'},
                {'name': 'Age', 'type': 'Number'},
                {'name': 'Sex', 'type': 'Text'},
                {'name': 'Implant Type', 'type': 'Text'}
            ])
            if trial not in p.trials:
                p.trials.append(trial)
            db.session.commit()
            trial_id = trial.id

        # Log in as test admin
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'test_admin', 'password': 'pass123'})

        # 1. Fetch Trial Dashboard EDC form for this patient
        res = self.client.get(f'/trials/{trial_id}/dashboard?cr_number=CR999888')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Verify pre-filled values in value="..." attributes
        self.assertIn('value="CR999888"', html)
        self.assertIn('value="Prefill Test Patient"', html)
        self.assertIn('value="55"', html)  # Numeric extraction for Age
        self.assertIn('value="Male"', html)

        # Verify readonly attribute is present for CR_No (identifying field)
        self.assertIn('name="field_0" value="CR999888"', html)
        self.assertIn('readonly', html)
        self.assertIn('Locked', html)

        # 2. Save EDC data with a custom value for Implant Type
        post_data = {
            'field_0': 'CR999888',
            'field_1': 'Prefill Test Patient',
            'field_2': '55',
            'field_3': 'Male',
            'field_4': 'Titanium Ceramic'
        }
        res_post = self.client.post(
            f'/trials/{trial_id}/patient/CR999888/edc',
            data=post_data,
            follow_redirects=True
        )
        self.assertEqual(res_post.status_code, 200)
        html_after = res_post.get_data(as_text=True)
        self.assertIn('Titanium Ceramic', html_after)

        # 3. Export Cohort CSV and verify EDC values are populated
        res_export = self.client.get(f'/trials/{trial_id}/export')
        self.assertEqual(res_export.status_code, 200)
        csv_text = res_export.get_data(as_text=True)
        self.assertIn('CR999888', csv_text)
        self.assertIn('Prefill Test Patient', csv_text)
        self.assertIn('Titanium Ceramic', csv_text)

    def test_login_must_change_password_interceptor(self):
        """Test first-time login interceptor when must_change_password is True."""
        with app.app_context():
            user = User.query.filter_by(username='invited_temp_user').first()
            if not user:
                user = User(
                    email='invited_temp@hospital.org',
                    username='invited_temp_user',
                    role='User',
                    status='Active',
                    must_change_password=True
                )
                user.set_password('TempPass1!')
                db.session.add(user)
            else:
                user.status = 'Active'
                user.must_change_password = True
                user.set_password('TempPass1!')
            db.session.commit()

        # Login attempt
        res = self.client.post('/login', data={
            'username': 'invited_temp_user',
            'password': 'TempPass1!'
        }, follow_redirects=False)

        # Must redirect to /setup-password without establishing standard authenticated session
        self.assertEqual(res.status_code, 302)
        self.assertIn('/setup-password', res.headers['Location'])

        # Directly accessing /dashboard must still be blocked (redirect to /login)
        res_dash = self.client.get('/dashboard')
        self.assertEqual(res_dash.status_code, 302)
        self.assertIn('/login', res_dash.headers['Location'])

    def test_setup_password_workflow(self):
        """Test /setup-password view for configuring permanent password."""
        with app.app_context():
            user = User.query.filter_by(username='setup_test_user').first()
            if not user:
                user = User(
                    email='setup_test@hospital.org',
                    username='setup_test_user',
                    role='Editor',
                    status='Active',
                    must_change_password=True
                )
                user.set_password('InitTemp123')
                db.session.add(user)
            else:
                user.status = 'Active'
                user.must_change_password = True
                user.set_password('InitTemp123')
            db.session.commit()

        # 1. Login sets session['setup_user_id']
        self.client.post('/login', data={'username': 'setup_test_user', 'password': 'InitTemp123'})

        # 2. GET setup-password view renders successfully
        res_get = self.client.get('/setup-password')
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('Configure Permanent Password', res_get.get_data(as_text=True))

        # 3. Mismatched confirmation fails
        res_mismatch = self.client.post('/setup-password', data={
            'temp_password': 'InitTemp123',
            'new_password': 'PermanentPass123',
            'confirm_password': 'DifferentPass123'
        }, follow_redirects=True)
        self.assertIn('do not match', res_mismatch.get_data(as_text=True))

        # 4. Valid permanent password configuration
        res_success = self.client.post('/setup-password', data={
            'temp_password': 'InitTemp123',
            'new_password': 'PermanentPass123',
            'confirm_password': 'PermanentPass123'
        }, follow_redirects=True)
        self.assertEqual(res_success.status_code, 200)
        self.assertIn('Permanent password configured successfully', res_success.get_data(as_text=True))

        # Verify database flag updated and authenticated
        with app.app_context():
            updated_user = User.query.filter_by(username='setup_test_user').first()
            self.assertFalse(updated_user.must_change_password)
            self.assertTrue(updated_user.check_password('PermanentPass123'))

        # Subsequent dashboard access succeeds
        res_dash = self.client.get('/dashboard')
        self.assertEqual(res_dash.status_code, 200)

    def test_change_password_authenticated(self):
        """Test authenticated user changing password via /change-password."""
        with app.app_context():
            user = User.query.filter_by(username='active_changer').first()
            if not user:
                user = User(
                    email='active_changer@hospital.org',
                    username='active_changer',
                    role='User',
                    status='Active',
                    must_change_password=False
                )
                user.set_password('OldPassword123')
                db.session.add(user)
            else:
                user.status = 'Active'
                user.must_change_password = False
                user.set_password('OldPassword123')
            db.session.commit()

        # Login
        self.client.post('/login', data={'username': 'active_changer', 'password': 'OldPassword123'})

        # Change password POST
        res_change = self.client.post('/change-password', data={
            'current_password': 'OldPassword123',
            'new_password': 'NewSecurePassword456',
            'confirm_password': 'NewSecurePassword456'
        }, follow_redirects=True)
        self.assertEqual(res_change.status_code, 200)
        self.assertIn('Password updated successfully', res_change.get_data(as_text=True))

        # Verify new password in database
        with app.app_context():
            updated = User.query.filter_by(username='active_changer').first()
            self.assertTrue(updated.check_password('NewSecurePassword456'))
            self.assertFalse(updated.check_password('OldPassword123'))

    def test_login_fetch_json_routing(self):
        """Test AJAX/fetch login returns JSON with redirect URL rather than naked 302 redirect."""
        # 1. Active normal login returns JSON with dashboard redirect
        res_active = self.client.post('/login', json={
            'username': 'rbac_admin@test.org',
            'password': 'AdminPass123!'
        }, headers={'Accept': 'application/json'})
        self.assertEqual(res_active.status_code, 200)
        data_active = res_active.get_json()
        self.assertTrue(data_active['success'])
        self.assertEqual(data_active['redirect'], '/dashboard')
        self.client.get('/logout')

        # 2. First-time login returns JSON redirecting to /setup-password
        with app.app_context():
            user = User.query.filter_by(username='invited_temp_user').first()
            if not user:
                user = User(
                    email='invited_temp@hospital.org',
                    username='invited_temp_user',
                    role='User',
                    status='Active',
                    must_change_password=True
                )
                user.set_password('TempPass1!')
                db.session.add(user)
            else:
                user.status = 'Active'
                user.must_change_password = True
                user.set_password('TempPass1!')
            db.session.commit()

        res_temp = self.client.post('/login', data={
            'username': 'invited_temp_user',
            'password': 'TempPass1!'
        }, headers={'Accept': 'application/json'})
        self.assertEqual(res_temp.status_code, 200)
        data_temp = res_temp.get_json()
        self.assertTrue(data_temp['success'])
        self.assertEqual(data_temp['redirect'], '/setup-password')
        self.assertTrue(data_temp['must_change_password'])

        # 3. Invalid credentials return JSON 401
        res_bad = self.client.post('/login', json={
            'username': 'rbac_admin@test.org',
            'password': 'WrongPassword999!'
        }, headers={'Accept': 'application/json'})
        self.assertEqual(res_bad.status_code, 401)
        data_bad = res_bad.get_json()
        self.assertFalse(data_bad['success'])
        self.assertIn('Invalid', data_bad['error'])

    def test_modality_nomenclature_mapping(self):
        """Test backend modality nomenclature dictionary and helper function."""
        self.assertEqual(MODALITY_DISPLAY_NAMES.get('CR'), 'X-Ray (CR)')
        self.assertEqual(MODALITY_DISPLAY_NAMES.get('CT'), 'CT Scan')
        self.assertEqual(MODALITY_DISPLAY_NAMES.get('MR'), 'MRI')
        self.assertEqual(MODALITY_DISPLAY_NAMES.get('DX'), 'Digital X-Ray')

        self.assertEqual(format_modality_name('CR'), 'X-Ray (CR)')
        self.assertEqual(format_modality_name('cr'), 'X-Ray (CR)')
        self.assertEqual(format_modality_name('CT'), 'CT Scan')
        self.assertEqual(format_modality_name('MR'), 'MRI')
        self.assertEqual(format_modality_name('DX'), 'Digital X-Ray')
        self.assertEqual(format_modality_name('UNKNOWN'), 'UNKNOWN')

    def test_generate_temp_password(self):
        """Test secure 8-character temporary password generation."""
        pwd = generate_temp_password(8)
        self.assertEqual(len(pwd), 8)
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))

    def test_invite_user_smtp_bypass_and_temp_password_generation(self):
        """Test user creation with temporary password generation, session storage for UI display, and must_change_password enforcement."""
        # 1. Login as Admin
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})

        # 2. Invite a new user
        test_email = 'dr_ortho_invite@hospital.org'
        res = self.client.post('/admin/users/invite', data={
            'email': test_email,
            'name': 'Dr. Ortho Invited',
            'role': 'Editor'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # 3. Verify user in database
        with app.app_context():
            created_user = User.query.filter_by(email=test_email).first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.role, 'Editor')
            self.assertEqual(created_user.status, 'Active')
            self.assertTrue(created_user.must_change_password)

            # Cleanup
            db.session.delete(created_user)
            db.session.commit()

    def test_delete_user_functionality_and_root_admin_protection(self):
        """Test deleting users with protection preventing root admins from deleting their own accounts."""
        # 1. Login as Admin
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})

        with app.app_context():
            admin = User.query.filter_by(email='rbac_admin@test.org').first()
            admin_id = admin.id

            # Create a target user to be deleted
            target = User.query.filter_by(username='delete_target_user').first()
            if not target:
                target = User(email='target@hospital.org', username='delete_target_user', role='User', status='Active')
                target.set_password('pass123')
                db.session.add(target)
                db.session.commit()
            target_id = target.id

        # 2. Admin cannot delete self
        res_self = self.client.post(f'/admin/delete_user/{admin_id}', follow_redirects=True)
        self.assertEqual(res_self.status_code, 200)
        self.assertIn(b'Security violation: You cannot delete your own root Admin account', res_self.data)
        with app.app_context():
            self.assertIsNotNone(db.session.get(User, admin_id))

        # 3. Admin can safely delete target user
        res_del = self.client.post(f'/admin/delete_user/{target_id}', follow_redirects=True)
        self.assertEqual(res_del.status_code, 200)
        self.assertIn(b'permanently deleted from the database', res_del.data)
        with app.app_context():
            self.assertIsNone(db.session.get(User, target_id))

    def test_patient_orthopedic_columns_and_migration(self):
        """Test diagnosis, ao_trauma_score, and anatomy columns on Patient model and lightweight ALTER TABLE migration."""
        with app.app_context():
            # Run startup migration check
            ensure_patient_schema_migrations()

            # Create test patient with orthopedic data
            p = Patient(
                cr_number='CR_ORTHO_EXP',
                patient_name='ORTHO TEST PATIENT',
                age='48 Yrs',
                gender='M',
                diagnosis='Distal Radius Fracture',
                ao_trauma_score='23-C3.2',
                anatomy='Distal Radius'
            )
            db.session.add(p)
            db.session.commit()

            fetched = Patient.query.filter_by(cr_number='CR_ORTHO_EXP').first()
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.diagnosis, 'Distal Radius Fracture')
            self.assertEqual(fetched.ao_trauma_score, '23-C3.2')
            self.assertEqual(fetched.anatomy, 'Distal Radius')

        # Login as Admin to edit metadata
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})
        res_edit = self.client.post('/patient/CR_ORTHO_EXP/edit', data={
            'patient_name': 'ORTHO TEST PATIENT UPDATED',
            'age': '49 Yrs',
            'gender': 'M',
            'diagnosis': 'Bimalleolar Ankle Fracture',
            'ao_trauma_score': '44-B2.1',
            'anatomy': 'Ankle'
        }, follow_redirects=True)
        self.assertEqual(res_edit.status_code, 200)

        with app.app_context():
            updated = Patient.query.filter_by(cr_number='CR_ORTHO_EXP').first()
            self.assertEqual(updated.diagnosis, 'Bimalleolar Ankle Fracture')
            self.assertEqual(updated.ao_trauma_score, '44-B2.1')
            self.assertEqual(updated.anatomy, 'Ankle')

            # Cleanup
            db.session.delete(updated)
            db.session.commit()


    def test_fluid_fullscreen_layout_templates(self):
        """Test that base, dashboard, trials, and admin templates use fluid width classes (w-full px-4 sm:px-6 lg:px-8)."""
        template_files = [
            'templates/base.html',
            'templates/dashboard.html',
            'templates/trials.html',
            'templates/trial_dashboard.html',
            'templates/admin_users.html',
            'templates/drive_sync.html'
        ]
        for tf in template_files:
            with open(tf, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('w-full', content, f"{tf} should have w-full fluid class")
            # Ensure no fixed container or max-w-7xl / max-w-screen-xl on main content containers
            self.assertNotIn('max-w-7xl', content, f"{tf} should not have fixed max-w-7xl")
            self.assertNotIn('max-w-screen-xl', content, f"{tf} should not have max-w-screen-xl")

    def test_clinical_profile_database_expansion(self):
        """Ensure Patient model and SQLite schema contain diagnosis, ao_grade, anatomy, and scanogram."""
        with app.app_context():
            ensure_patient_schema_migrations()
            # Verify columns exist on Patient class
            self.assertTrue(hasattr(Patient, 'diagnosis'))
            self.assertTrue(hasattr(Patient, 'ao_grade'))
            self.assertTrue(hasattr(Patient, 'anatomy'))
            self.assertTrue(hasattr(Patient, 'scanogram'))

            p = Patient(
                cr_number='CR_CLINICAL_TEST',
                patient_name='Clinical Test Patient',
                age='55 Yrs',
                gender='F',
                diagnosis='Intertrochanteric Femur Fracture',
                ao_grade='31-A2',
                anatomy='Right',
                scanogram='Yes'
            )
            db.session.add(p)
            db.session.commit()

            fetched = Patient.query.filter_by(cr_number='CR_CLINICAL_TEST').first()
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.diagnosis, 'Intertrochanteric Femur Fracture')
            self.assertEqual(fetched.ao_grade, '31-A2')
            self.assertEqual(fetched.anatomy, 'Right')
            self.assertEqual(fetched.scanogram, 'Yes')

            # Cleanup
            db.session.delete(fetched)
            db.session.commit()

    def test_role_protected_clinical_profile_edit_route(self):
        """Test that /patient/edit/ route strictly requires Admin or Editor role and rejects Viewer with 403 Forbidden."""
        with app.app_context():
            # Create a test patient
            p = Patient.query.filter_by(cr_number='CR_ROLE_EDIT').first()
            if not p:
                p = Patient(
                    cr_number='CR_ROLE_EDIT',
                    patient_name='Role Edit Target',
                    diagnosis='Initial Diagnosis',
                    ao_grade='NA',
                    anatomy='Spine',
                    scanogram='No'
                )
                db.session.add(p)
                db.session.commit()

        # 1. Viewer attempt -> Must return 403 Forbidden
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_viewer@test.org', 'password': 'ViewerPass123!'})
        res_viewer = self.client.post('/patient/edit/', data={
            'cr_number': 'CR_ROLE_EDIT',
            'diagnosis': 'Viewer Attempt Hack',
            'ao_grade': '31-A1',
            'anatomy': 'Left',
            'scanogram': 'Yes'
        })
        self.assertEqual(res_viewer.status_code, 403, "Viewer must be forbidden from updating clinical profile with 403")

        # 2. Editor attempt -> Must succeed
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})
        res_editor = self.client.post('/patient/edit/', data={
            'cr_number': 'CR_ROLE_EDIT',
            'diagnosis': 'Subtrochanteric Fracture',
            'ao_grade': '32-A1',
            'anatomy': 'Right',
            'scanogram': 'Yes'
        }, follow_redirects=True)
        self.assertEqual(res_editor.status_code, 200)

        with app.app_context():
            updated = Patient.query.filter_by(cr_number='CR_ROLE_EDIT').first()
            self.assertEqual(updated.diagnosis, 'Subtrochanteric Fracture')
            self.assertEqual(updated.ao_grade, '32-A1')
            self.assertEqual(updated.anatomy, 'Right')
            self.assertEqual(updated.scanogram, 'Yes')

        # 3. Admin attempt -> Must succeed
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_admin@test.org', 'password': 'AdminPass123!'})
        res_admin = self.client.post('/patient/edit/', data={
            'cr_number': 'CR_ROLE_EDIT',
            'diagnosis': 'Femoral Neck Fracture',
            'ao_grade': '31-B2',
            'anatomy': 'Bilateral',
            'scanogram': 'No'
        }, follow_redirects=True)
        self.assertEqual(res_admin.status_code, 200)

        with app.app_context():
            final_p = Patient.query.filter_by(cr_number='CR_ROLE_EDIT').first()
            self.assertEqual(final_p.diagnosis, 'Femoral Neck Fracture')
            self.assertEqual(final_p.ao_grade, '31-B2')
            self.assertEqual(final_p.anatomy, 'Bilateral')
            self.assertEqual(final_p.scanogram, 'No')

            # Cleanup
            db.session.delete(final_p)
            db.session.commit()

    def test_frontend_edit_profile_button_visibility_and_modal(self):
        """Test that Edit Profile button is invisible to Viewers and visible to Admins/Editors, and modal exists."""
        # 1. Viewer view: "Edit Profile" button must not appear in HTML
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_viewer@test.org', 'password': 'ViewerPass123!'})
        res_viewer = self.client.get('/dashboard')
        self.assertEqual(res_viewer.status_code, 200)
        self.assertNotIn(b'Edit Profile', res_viewer.data)
        self.assertNotIn(b'id="clinicalProfileModal"', res_viewer.data)

        # 2. Editor view: "Edit Profile" button and modal must be present
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})
        res_editor = self.client.get('/dashboard')
        self.assertEqual(res_editor.status_code, 200)
        self.assertIn(b'Edit Profile', res_editor.data)
        self.assertIn(b'id="clinicalProfileModal"', res_editor.data)
        self.assertIn(b'clinical_ao_grade', res_editor.data)
        self.assertIn(b'clinical_anatomy', res_editor.data)
        self.assertIn(b'clinical_scanogram', res_editor.data)
        self.assertIn(b'NA (Not Applicable)', res_editor.data)
        self.assertIn(b'clinical_patient_name', res_editor.data)

    def test_strict_12digit_cr_extraction_and_name_sanitization(self):
        """Test strict 12-digit CR number regex extraction and patient name sanitization."""
        # 1. Direct 12-digit starting with year
        self.assertEqual(extract_cr_number('202410158941', ''), '202410158941')
        self.assertEqual(extract_strict_12digit_cr('202410158941', ''), '202410158941')

        # 2. Embedded in patient name
        self.assertEqual(extract_cr_number('', 'RATHORE SUNIL 201806126956'), '201806126956')
        self.assertEqual(extract_cr_number('', 'DOE^JOHN 202311223344'), '202311223344')

        # 3. Embedded with CR prefix
        self.assertEqual(extract_cr_number('CR-201502060432', 'ANONYMOUS'), '201502060432')

        # 4. In filepath
        self.assertEqual(extract_cr_number('', '', '/data/dicoms/201904171476/slice.dcm'), '201904171476')

        # 5. Patient Name Sanitization: Stripping 12-digit CR completely
        self.assertEqual(clean_patient_name('RATHORE SUNIL 201806126956'), 'RATHORE SUNIL')
        self.assertEqual(clean_patient_name('DOE^JOHN 202311223344'), 'JOHN DOE')
        self.assertEqual(clean_patient_name('201806126956 RATHORE SUNIL'), 'RATHORE SUNIL')
        self.assertEqual(clean_patient_name('201806126956'), 'Anonymous')

    def test_retroactive_cr_migration(self):
        """Test retroactive database migration extracting 12-digit CR and updating foreign keys."""
        with app.app_context():
            # Seed a dirty patient
            dirty_p = Patient(
                cr_number='CR_MIGRATE_OLD',
                patient_name='KAPOOR^RAVI 202410158941',
                age='45',
                gender='M',
                diagnosis='Distal Radius Fracture'
            )
            db.session.add(dirty_p)
            db.session.commit()

            # Add associated scan
            scan = Scan(
                cr_number='CR_MIGRATE_OLD',
                modality='CR',
                drive_file_id='local_migrate_test.dcm'
            )
            db.session.add(scan)
            db.session.commit()

            # Run retroactive migration
            migrated_count = run_retroactive_cr_migration()
            self.assertGreaterEqual(migrated_count, 1)

            # Verify patient record has migrated to 12-digit CR
            migrated_p = db.session.get(Patient, '202410158941')
            self.assertIsNotNone(migrated_p)
            self.assertEqual(migrated_p.patient_name, 'RAVI KAPOOR')
            self.assertEqual(migrated_p.diagnosis, 'Distal Radius Fracture')

            # Old record should no longer exist
            old_p = db.session.get(Patient, 'CR_MIGRATE_OLD')
            self.assertIsNone(old_p)

            # Scan record must now reference new 12-digit CR
            updated_scan = Scan.query.filter_by(drive_file_id='local_migrate_test.dcm').first()
            self.assertIsNotNone(updated_scan)
            self.assertEqual(updated_scan.cr_number, '202410158941')

    def test_edit_patient_name_route(self):
        """Test editing patient name via role-protected /patient/edit/ route."""
        with app.app_context():
            p = Patient(
                cr_number='202311223344',
                patient_name='Original Name',
                age='50',
                gender='F'
            )
            db.session.add(p)
            db.session.commit()

        # 1. Viewer attempt -> 403 Forbidden
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_viewer@test.org', 'password': 'ViewerPass123!'})
        res_viewer = self.client.post('/patient/edit/', data={
            'cr_number': '202311223344',
            'patient_name': 'Hacked Name'
        })
        self.assertEqual(res_viewer.status_code, 403)

        # 2. Editor attempt -> Success
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})
        res_editor = self.client.post('/patient/edit/', data={
            'cr_number': '202311223344',
            'patient_name': 'Corrected Medical Name',
            'diagnosis': 'Femur Shaft Fracture'
        }, follow_redirects=True)
        self.assertEqual(res_editor.status_code, 200)

        with app.app_context():
            updated = db.session.get(Patient, '202311223344')
            self.assertEqual(updated.patient_name, 'Corrected Medical Name')
            self.assertEqual(updated.diagnosis, 'Femur Shaft Fracture')

    def test_trial_edc_schema_management(self):
        """Test dynamic Trial EDC schema mutations (rename, type change, delete) and role protection."""
        import json
        with app.app_context():
            # Seed patient
            p = Patient.query.filter_by(cr_number='202410158941').first()
            if not p:
                p = Patient(cr_number='202410158941', patient_name='Schema Test Patient')
                db.session.add(p)

            # Seed trial
            trial = Trial(
                trial_name='SCHEMA-MGMT-TRIAL',
                target_sample_size=30,
                description='EDC Schema Management Protocol'
            )
            initial_schema = [
                {'name': 'ROM Pre-Op', 'type': 'Text'},
                {'name': 'VAS Pain Score', 'type': 'Number'},
                {'name': 'To Be Deleted Field', 'type': 'Text'}
            ]
            trial.set_data_schema(initial_schema)
            db.session.add(trial)
            db.session.commit()
            trial_id = trial.id

            # Add TrialPatientData
            tpd = TrialPatientData(
                trial_id=trial_id,
                cr_number='202410158941',
                data_json=json.dumps({
                    'ROM Pre-Op': '80 degrees',
                    'VAS Pain Score': 7,
                    'To Be Deleted Field': 'delete me'
                })
            )
            db.session.add(tpd)
            db.session.commit()

        # 1. Viewer cannot update trial schema -> 403
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_viewer@test.org', 'password': 'ViewerPass123!'})
        res_v = self.client.post(f'/trials/{trial_id}/schema', json={
            'fields': [{'name': 'ROM', 'type': 'Text', 'old_name': 'ROM Pre-Op'}]
        })
        self.assertEqual(res_v.status_code, 403)

        # 2. Editor updates schema:
        # - Renames 'ROM Pre-Op' to 'Range of Motion' (type 'Number')
        # - Changes 'VAS Pain Score' type to 'File/PDF/Image'
        # - Deletes 'To Be Deleted Field'
        self.client.post('/logout')
        self.client.post('/login', data={'username': 'rbac_editor@test.org', 'password': 'EditorPass123!'})
        res_e = self.client.post(f'/trials/{trial_id}/schema', json={
            'fields': [
                {'name': 'Range of Motion', 'type': 'Number', 'old_name': 'ROM Pre-Op'},
                {'name': 'VAS Pain Score', 'type': 'File/PDF/Image', 'old_name': 'VAS Pain Score'}
            ],
            'deleted': ['To Be Deleted Field']
        })
        self.assertEqual(res_e.status_code, 200)
        data = res_e.get_json()
        self.assertTrue(data.get('success'))

        with app.app_context():
            updated_trial = db.session.get(Trial, trial_id)
            new_schema = updated_trial.get_data_schema()
            self.assertEqual(len(new_schema), 2)
            self.assertEqual(new_schema[0]['name'], 'Range of Motion')
            self.assertEqual(new_schema[0]['type'], 'Number')
            self.assertEqual(new_schema[1]['name'], 'VAS Pain Score')
            self.assertEqual(new_schema[1]['type'], 'File')

            # Verify patient data key was safely renamed and deleted key removed
            updated_tpd = TrialPatientData.query.filter_by(trial_id=trial_id, cr_number='202410158941').first()
            p_data = json.loads(updated_tpd.data_json)
            self.assertEqual(p_data.get('Range of Motion'), '80 degrees')
            self.assertNotIn('ROM Pre-Op', p_data)
            self.assertNotIn('To Be Deleted Field', p_data)

    def test_trial_dashboard_sidebar_and_schema_editor_ui(self):
        """Test full-height sidebar classes and schema editor modal presence in template."""
        with open(os.path.join(app.root_path, 'templates', 'trial_dashboard.html'), 'r', encoding='utf-8') as f:
            html = f.read()

        # Requirement 4: items-stretch parent and h-full sidebar
        self.assertIn('items-stretch', html)
        self.assertIn('flex flex-col h-full', html)
        self.assertIn('id="patientSidebarList"', html)

        # Requirement 3: Edit Trial Schema modal and controls
        self.assertIn('id="editSchemaModal"', html)
        self.assertIn('id="editSchemaBtn"', html)
        self.assertIn('addNewSchemaRow', html)
        self.assertIn('deleteSchemaRow', html)
        self.assertIn('File/PDF/Image', html)


if __name__ == '__main__':
    unittest.main()


