import os
import io
import unittest
from app import app, db
from models import User, Patient, Scan, Trial, CustomData
from drive_service import extract_cr_number, clean_patient_name, clean_age, parse_dicom_bytes

TEST_CR_NUMBERS = [
    'CR990011', 'CR778899', 'CR334455', 'CR889900', 'CR888111',
    'CR777000', 'CR556677', 'CR_SERIES_TEST', 'CR_STREAM_TEST'
]
TEST_TRIALS = ['HIP-ARTHROPLASTY-2026', 'EXPORT-TRIAL']

class OrthoregTestCase(unittest.TestCase):

    def _cleanup_test_data(self):
        with app.app_context():
            scans = Scan.query.filter(Scan.cr_number.in_(TEST_CR_NUMBERS)).all()
            for s in scans:
                db.session.delete(s)
            CustomData.query.filter(CustomData.cr_number.in_(TEST_CR_NUMBERS)).delete(synchronize_session=False)
            patients = Patient.query.filter(Patient.cr_number.in_(TEST_CR_NUMBERS)).all()
            for p in patients:
                p.trials = []
                db.session.delete(p)
            trials = Trial.query.filter(Trial.trial_name.in_(TEST_TRIALS)).all()
            for t in trials:
                t.patients = []
                db.session.delete(t)
            try:
                for cr in TEST_CR_NUMBERS:
                    db.session.execute(db.text("DELETE FROM patient_trials WHERE patient_cr = :cr"), {"cr": cr})
                for tname in TEST_TRIALS:
                    db.session.execute(db.text("DELETE FROM trials WHERE trial_name = :tn"), {"tn": tname})
            except Exception:
                pass
            db.session.commit()

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

if __name__ == '__main__':
    unittest.main()
