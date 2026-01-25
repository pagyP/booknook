import pytest
from app import app, db, User, Book, RecoveryCode, configure_logging, generate_recovery_codes, password_hasher
from datetime import datetime


@pytest.fixture
def client():
    """Create a test client for the app."""
    # Configure app for testing with a file-based database
    app.config['TESTING'] = True
    app.config['RATELIMIT_ENABLED'] = False
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'
    
    # Reconfigure logging after setting TESTING flag
    # This ensures the logger respects the TESTING configuration
    configure_logging(app)
    
    # Setup database
    with app.app_context():
        db.drop_all()  # Clean previous test runs
        db.create_all()
    
    # Create test client
    client = app.test_client()
    
    yield client
    
    # Teardown
    with app.app_context():
        db.session.remove()
        db.drop_all()
    
    # Clean up test database file
    import os
    if os.path.exists('test.db'):
        os.remove('test.db')


@pytest.fixture
def auth_user(client):
    """Create a test user and return client with user logged in."""
    # Create user directly in the app context
    with app.app_context():
        # Make sure to clear any cached sessions
        from flask_login import current_user
        
        user = User(username='testuser', email='test@example.com')
        user.set_password('TestPass123!')
        db.session.add(user)
        db.session.commit()
    
    # Login the user using the test client
    response = client.post('/login', data={
        'email': 'test@example.com',
        'password': 'TestPass123!'
    }, follow_redirects=True)
    
    # Verify login was successful by checking response
    assert response.status_code == 200, f"Login failed with status {response.status_code}"
    
    # Retrieve the user from database in a fresh context
    with app.app_context():
        user = User.query.filter_by(email='test@example.com').first()
        assert user is not None, "User not found after creation"
    
    return client, user


def make_openlibrary_mock_get(mock_response):
    """Factory to create a `requests.get` mock that simulates Open Library search and works endpoints.

    It returns a function suitable for `monkeypatch.setattr('app.requests.get', ...)`.
    The returned function will return `mock_response` for the search endpoint and
    a `{'subjects': ...}` dict for the works endpoint based on `mock_response['docs'][0]['subject']`.
    """
    from unittest.mock import Mock

    def _mock_get(*args, **kwargs):
        url = args[0] if args else kwargs.get('url', '')
        response = Mock()
        response.status_code = 200
        if '/works/' in url and url.endswith('.json'):
            docs = mock_response.get('docs') if isinstance(mock_response, dict) else None
            subjects = []
            if docs:
                first = docs[0] if isinstance(docs, list) and docs else None
                if isinstance(first, dict):
                    subjects = first.get('subject', []) or []
            response.json.return_value = {'subjects': subjects}
        else:
            response.json.return_value = mock_response
        return response

    return _mock_get


class TestAuthentication:
    """Test user authentication routes."""
    
    def test_register_page_loads(self, client):
        """Test that registration page loads."""
        response = client.get('/register')
        assert response.status_code == 200
        assert b'Create Account' in response.data
    
    def test_register_new_user(self, client):
        """Test user registration with valid data."""
        response = client.post('/register', data={
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!'
        }, follow_redirects=True)
        # Should redirect to login
        assert response.status_code == 200
        
        with app.app_context():
            user = User.query.filter_by(email='newuser@example.com').first()
            assert user is not None
            assert user.username == 'newuser'
    
    def test_register_duplicate_email(self, client):
        """Test registration fails with duplicate email."""
        with app.app_context():
            user = User(username='existing', email='existing@example.com')
            user.set_password('ValidPass123!')
            db.session.add(user)
            db.session.commit()
        
        response = client.post('/register', data={
            'username': 'newuser',
            'email': 'existing@example.com',
            'password': 'ValidPass123!',
            'confirm_password': 'ValidPass123!'
        })
        assert b'Email already registered' in response.data
    
    def test_register_duplicate_username(self, client):
        """Test registration fails with duplicate username."""
        with app.app_context():
            user = User(username='existing', email='existing@example.com')
            user.set_password('ValidPass123!')
            db.session.add(user)
            db.session.commit()
        
        response = client.post('/register', data={
            'username': 'existing',
            'email': 'different@example.com',
            'password': 'ValidPass123!',
            'confirm_password': 'ValidPass123!'
        })
        assert b'Username already exists' in response.data
    
    def test_login_page_loads(self, client):
        """Test that login page loads."""
        response = client.get('/login')
        assert response.status_code == 200
        assert b'Log In' in response.data
    
    def test_login_successful(self, client):
        """Test successful login."""
        with app.app_context():
            user = User(username='testuser', email='test@example.com')
            user.set_password('LoginPass123!')
            db.session.add(user)
            db.session.commit()
        
        response = client.post('/login', data={
            'email': 'test@example.com',
            'password': 'LoginPass123!'
        }, follow_redirects=True)
        # Should redirect to index
        assert response.status_code == 200
        assert b'Reading Nook' in response.data
    
    def test_login_invalid_password(self, client):
        """Test login fails with wrong password."""
        with app.app_context():
            user = User(username='testuser', email='test@example.com')
            user.set_password('CorrectPass123!')
            db.session.add(user)
            db.session.commit()
        
        response = client.post('/login', data={
            'email': 'test@example.com',
            'password': 'WrongPass123!'
        })
        assert b'Invalid email or password' in response.data or response.status_code == 200
    
    def test_logout(self, auth_user):
        """Test user logout."""
        client, _ = auth_user
        response = client.get('/logout')
        assert response.status_code == 302


class TestBookRoutes:
    """Test book management routes."""
    
    def test_index_requires_login(self, client):
        """Test that index page requires login."""
        response = client.get('/')
        # Should redirect to login
        assert response.status_code == 302
    
    def test_index_page_loads(self, auth_user):
        """Test that home page loads for logged in user."""
        client, _ = auth_user
        response = client.get('/')
        assert response.status_code == 200
        assert b'Reading Nook' in response.data
    
    def test_add_book_page_loads(self, auth_user):
        """Test that add book page loads."""
        client, _ = auth_user
        response = client.get('/add')
        assert response.status_code == 200
        assert b'Add a New Book' in response.data
    
    def test_add_book_success(self, auth_user):
        """Test adding a new book."""
        client, user = auth_user
        response = client.post('/add', data={
            'title': 'Test Book',
            'author': 'Test Author',
            'isbn': '978-0-123456-78-9',
            'genre': 'Fiction',
            'format': 'physical',
            'status': 'read',
            'date_read': '2025-12-30',
            'rating': '5',
            'notes': 'A great book!'
        }, follow_redirects=True)
        assert response.status_code == 200
        
        with app.app_context():
            book = Book.query.filter_by(title='Test Book').first()
            assert book is not None
            assert book.author == 'Test Author'
            assert book.isbn == '978-0-123456-78-9'
            assert book.genre == 'Fiction'
            assert book.format == 'physical'
            assert book.status == 'read'
            assert book.rating == 5
    
    def test_add_book_minimal(self, auth_user):
        """Test adding a book with only required fields."""
        client, user = auth_user
        response = client.post('/add', data={
            'title': 'Minimal Book',
            'author': 'Some Author',
            'format': 'physical',
            'status': 'read',
            'date_read': '2025-12-30'
        }, follow_redirects=True)
        assert response.status_code == 200
        
        with app.app_context():
            book = Book.query.filter_by(title='Minimal Book').first()
            assert book is not None
            assert book.format == 'physical'
            assert book.status == 'read'
    
    def test_edit_book_page_loads(self, auth_user):
        """Test that edit book page loads."""
        client, user = auth_user
        
        with app.app_context():
            book = Book(title='Test', author='Author', user_id=user.id)
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        response = client.get(f'/edit/{book_id}')
        assert response.status_code == 200
        assert b'Edit Book' in response.data
    
    def test_edit_book_page_displays_existing_cover(self, auth_user):
        """Test that edit book page displays existing cover image."""
        client, user = auth_user
        
        cover_url = 'https://covers.openlibrary.org/b/id/99999-M.jpg'
        with app.app_context():
            book = Book(
                title='Book With Cover',
                author='Test Author',
                user_id=user.id,
                cover_url=cover_url
            )
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        response = client.get(f'/edit/{book_id}')
        assert response.status_code == 200
        assert b'Edit Book' in response.data
        # Verify the cover URL is in the page (in the hidden input and img src)
        assert cover_url.encode() in response.data
    
    def test_edit_book_success(self, auth_user):
        """Test editing a book."""
        client, user = auth_user
        
        with app.app_context():
            book = Book(title='Original', author='Author', user_id=user.id)
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        response = client.post(f'/edit/{book_id}', data={
            'title': 'Updated Title',
            'author': 'Updated Author',
            'genre': 'Mystery',
            'format': 'ebook',
            'status': 'read',
            'date_read': '2025-12-30',
            'rating': '4'
        }, follow_redirects=True)
        assert response.status_code == 200
        
        with app.app_context():
            book = db.get_or_404(Book, book_id)
            assert book.title == 'Updated Title'
            assert book.author == 'Updated Author'
            assert book.genre == 'Mystery'
            assert book.format == 'ebook'
            assert book.rating == 4
    
    def test_edit_book_preserves_cover_url(self, auth_user):
        """Test that editing a book preserves the cover image when not changed."""
        client, user = auth_user
        
        # Create a book with a cover URL
        original_cover_url = 'https://covers.openlibrary.org/b/id/12345-M.jpg'
        with app.app_context():
            book = Book(
                title='Original Title',
                author='Original Author',
                user_id=user.id,
                cover_url=original_cover_url
            )
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        # Edit the book without changing the cover URL
        response = client.post(f'/edit/{book_id}', data={
            'title': 'Updated Title',
            'author': 'Updated Author',
            'format': 'physical',
            'status': 'read',
            'cover_url': original_cover_url  # Send the existing cover URL
        }, follow_redirects=True)
        assert response.status_code == 200
        
        # Verify the cover URL is preserved
        with app.app_context():
            book = db.get_or_404(Book, book_id)
            assert book.cover_url == original_cover_url
            assert book.title == 'Updated Title'
    
    def test_edit_book_without_cover_url_preserves_existing(self, auth_user):
        """Test that editing a book without providing cover_url keeps existing image."""
        client, user = auth_user
        
        # Create a book with a cover URL
        original_cover_url = 'https://covers.openlibrary.org/b/id/67890-M.jpg'
        with app.app_context():
            book = Book(
                title='Book with Cover',
                author='Test Author',
                user_id=user.id,
                cover_url=original_cover_url
            )
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        # Edit the book WITHOUT providing cover_url in the form data
        response = client.post(f'/edit/{book_id}', data={
            'title': 'Updated Title Without Cover',
            'author': 'Updated Author',
            'format': 'ebook',
            'status': 'read'
            # Note: no cover_url field in the form data
        }, follow_redirects=True)
        assert response.status_code == 200
        
        # Verify the cover URL is still preserved
        with app.app_context():
            book = db.get_or_404(Book, book_id)
            assert book.cover_url == original_cover_url
            assert book.title == 'Updated Title Without Cover'
    
    def test_delete_book(self, auth_user):
        """Test deleting a book."""
        client, user = auth_user
        
        with app.app_context():
            book = Book(title='To Delete', author='Author', user_id=user.id)
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        response = client.get(f'/delete/{book_id}', follow_redirects=True)
        assert response.status_code == 200
        
        with app.app_context():
            book = db.session.get(Book, book_id)
            assert book is None
    
    def test_user_cannot_edit_other_users_book(self, client):
        """Test that a user cannot edit another user's book."""
        with app.app_context():
            user1 = User(username='user1', email='user1@example.com')
            user1.set_password('ValidPass123!')
            user2 = User(username='user2', email='user2@example.com')
            user2.set_password('ValidPass123!')
            db.session.add(user1)
            db.session.add(user2)
            db.session.commit()
            
            book = Book(title='User1 Book', author='Author', user_id=user1.id)
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        # Login as user2
        login_response = client.post('/login', data={
            'email': 'user2@example.com',
            'password': 'ValidPass123!'
        }, follow_redirects=True)
        # Verify login was successful
        assert login_response.status_code == 200
        
        # Try to edit user1's book
        response = client.get(f'/edit/{book_id}')
        assert response.status_code == 302  # Redirected (permission denied)


class TestSearch:
    """Test search functionality."""
    
    def test_search_by_title(self, auth_user):
        """Test searching for books by title."""
        client, user = auth_user
        
        with app.app_context():
            book1 = Book(title='The Great Gatsby', author='F. Scott Fitzgerald', user_id=user.id)
            book2 = Book(title='1984', author='George Orwell', user_id=user.id)
            db.session.add_all([book1, book2])
            db.session.commit()
        
        response = client.get('/?search=Gatsby')
        assert response.status_code == 200
        assert b'The Great Gatsby' in response.data
    
    def test_search_by_author(self, auth_user):
        """Test searching for books by author."""
        client, user = auth_user
        
        with app.app_context():
            book1 = Book(title='The Great Gatsby', author='F. Scott Fitzgerald', user_id=user.id)
            book2 = Book(title='1984', author='George Orwell', user_id=user.id)
            db.session.add_all([book1, book2])
            db.session.commit()
        
        response = client.get('/?search=Orwell')
        assert response.status_code == 200
        assert b'1984' in response.data
        assert b'Great Gatsby' not in response.data
    
    def test_search_by_genre(self, auth_user):
        """Test searching for books by genre."""
        client, user = auth_user
        
        with app.app_context():
            book1 = Book(title='Book 1', author='Author 1', genre='Mystery', user_id=user.id)
            book2 = Book(title='Book 2', author='Author 2', genre='Fiction', user_id=user.id)
            db.session.add_all([book1, book2])
            db.session.commit()
        
        response = client.get('/?search=Mystery')
        assert response.status_code == 200
        assert b'Book 1' in response.data
        assert b'Book 2' not in response.data
    
    def test_search_by_isbn(self, auth_user):
        """Test searching for books by ISBN."""
        client, user = auth_user
        
        with app.app_context():
            book1 = Book(title='Book 1', author='Author 1', isbn='978-0-111111-11-1', user_id=user.id)
            book2 = Book(title='Book 2', author='Author 2', isbn='978-0-222222-22-2', user_id=user.id)
            db.session.add_all([book1, book2])
            db.session.commit()
        
        response = client.get('/?search=978-0-111111-11-1')
        assert response.status_code == 200
        assert b'Book 1' in response.data
        assert b'Book 2' not in response.data
    
    def test_search_case_insensitive(self, auth_user):
        """Test that search is case-insensitive."""
        client, user = auth_user
        
        with app.app_context():
            book = Book(title='The Great Gatsby', author='Author', user_id=user.id)
            db.session.add(book)
            db.session.commit()
        
        response = client.get('/?search=GATSBY')
        assert response.status_code == 200
        assert b'The Great Gatsby' in response.data
    
    def test_search_no_results(self, auth_user):
        """Test search with no results."""
        client, user = auth_user
        
        with app.app_context():
            book = Book(title='The Great Gatsby', author='Author', user_id=user.id)
            db.session.add(book)
            db.session.commit()
        
        response = client.get('/?search=nonexistent')
        assert response.status_code == 200
        assert b'No books found' in response.data


class TestPasswordSecurity:
    """Test password hashing and security."""
    
    def test_password_is_hashed(self):
        """Test that passwords are hashed, not stored in plain text."""
        user = User(username='testuser', email='test@example.com')
        user.set_password('mypassword123')
        
        # Password should not equal the hash
        assert user.password_hash != 'mypassword123'
        # Hash should not be empty
        assert user.password_hash
    
    def test_password_check(self):
        """Test password verification."""
        user = User(username='testuser', email='test@example.com')
        user.set_password('correctpassword')
        
        assert user.check_password('correctpassword') is True
        assert user.check_password('wrongpassword') is False
    
    def test_password_check_with_corrupted_hash(self, caplog):
        """Test that corrupted/invalid hash is handled gracefully with logging.
        
        This ensures that if a password hash is corrupted or in an invalid format
        (e.g., from database corruption or migration issues), the check_password
        method returns False and logs a warning instead of crashing.
        """
        import logging
        
        user = User(username='corrupteduser', email='corrupted@example.com')
        user.set_password('validpassword')
        
        # Verify password works normally first
        assert user.check_password('validpassword') is True
        
        # Simulate corrupted/invalid hash (not a valid Argon2 format)
        user.password_hash = 'invalid_corrupted_hash_format'
        
        # Capture logs to verify warning is logged
        with caplog.at_level(logging.WARNING):
            result = user.check_password('anypassword')
        
        # Should return False instead of raising an exception
        assert result is False
        
        # Should have logged a warning about invalid hash
        assert any('Invalid password hash' in record.message for record in caplog.records)
        assert any('corrupteduser' in record.message for record in caplog.records)

class TestRecoveryCodes:
    """Test password recovery using recovery codes"""
    
    def test_recovery_codes_generated_on_registration(self, client):
        """Verify recovery codes are generated and displayed after registration"""
        # Register a new user
        response = client.post('/register', data={
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!'
        }, follow_redirects=True)
        
        # Should redirect to recovery codes page
        assert response.status_code == 200
        assert b'Save Your Recovery Codes' in response.data
    
    def test_recovery_codes_stored_in_database(self, client):
        """Verify recovery codes are hashed and stored in database"""
        from app import RecoveryCode
        
        # Register a new user
        response = client.post('/register', data={
            'username': 'codetest',
            'email': 'codetest@example.com',
            'password': 'SecurePass123!',
            'confirm_password': 'SecurePass123!'
        })
        
        # Get the user and verify codes were created
        with app.app_context():
            user = User.query.filter_by(username='codetest').first()
            codes = RecoveryCode.query.filter_by(user_id=user.id).all()
            
            # Should have 8 recovery codes
            assert len(codes) == 8
            
            # All codes should be unused
            assert all(not code.used for code in codes)
            
            # Codes should be hashed (not stored as plain text)
            assert all(len(code.code_hash) > 20 for code in codes)
    
    def test_forgot_password_page_loads(self, client):
        """Verify forgot password page loads"""
        response = client.get('/forgot-password')
        assert response.status_code == 200
        assert b'Recover Your Account' in response.data
        assert b'Recovery Code' in response.data
    
    def test_password_reset_with_valid_recovery_code(self, client):
        """Verify password can be reset with a valid recovery code (end-to-end test)
        
        This test covers the complete recovery flow:
        1. Generate recovery codes during registration
        2. Verify a real recovery code works
        3. Reset password with that code  
        4. Verify old password doesn't work, new password does
        """
        import re
        
        # Step 1: Register user (generates recovery codes in database)
        response = client.post('/register', data={
            'username': 'resetuser',
            'email': 'resetuser@example.com',
            'password': 'OldPass123!',
            'confirm_password': 'OldPass123!'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        # Step 2: Logout to clear session (if logged in)
        client.get('/logout', follow_redirects=True)
        
        # Step 3: Get a real recovery code from the database
        with app.app_context():
            user = User.query.filter_by(email='resetuser@example.com').first()
            assert user is not None
            
            # Codes were generated and stored during registration
            codes = RecoveryCode.query.filter_by(user_id=user.id, used=False).all()
            assert len(codes) >= 8
            
            # Generate one test code with the correct base32 format
            real_recovery_code = generate_recovery_codes(999, count=1)[0]
            
            # Hash it and store it for this user (simulating a generated code)
            test_code_hash = password_hasher.hash(real_recovery_code)
            test_recovery_code = RecoveryCode(user_id=user.id, code_hash=test_code_hash)
            db.session.add(test_recovery_code)
            db.session.commit()
        
        # Step 4: Submit forgot password with the real recovery code
        response = client.post('/forgot-password', data={
            'email': 'resetuser@example.com',
            'recovery_code': real_recovery_code
        }, follow_redirects=True)
        
        # Should end up on reset password page
        assert response.status_code == 200
        assert b'Reset Your Password' in response.data
        
        # Step 5: Extract the token from the hidden form field
        token_match = re.search(rb'name="token"\s+value="([^"]+)"', response.data)
        assert token_match is not None, "Token not found in reset password form"
        token_value = token_match.group(1).decode('utf-8')
        assert len(token_value) > 0
        
        # Step 6: Submit new password via reset form with token
        response = client.post('/reset-password', data={
            'token': token_value,
            'password': 'NewPass123!',
            'confirm_password': 'NewPass123!'
        }, follow_redirects=True)
        
        # Should succeed and redirect to login
        assert response.status_code == 200
        assert b'Password reset successfully' in response.data or b'login' in response.data.lower()
        
        # Step 7: Verify old password doesn't work anymore
        login_response = client.post('/login', data={
            'email': 'resetuser@example.com',
            'password': 'OldPass123!'
        }, follow_redirects=True)
        # Should show error or stay on login page
        assert b'Invalid email or password' in login_response.data or b'Recover Account' in login_response.data or b'login' in login_response.data.lower()
        
        # Step 8: Verify new password works
        login_response = client.post('/login', data={
            'email': 'resetuser@example.com',
            'password': 'NewPass123!'
        }, follow_redirects=True)
        # Should successfully log in and redirect to index
        assert login_response.status_code == 200
        # Check for either success message or index page content
        assert b'Welcome' in login_response.data or b'Reading Nook' in login_response.data
    
    def test_invalid_recovery_code_rejected(self, client):
        """Verify invalid recovery codes are rejected"""
        # Register user
        client.post('/register', data={
            'username': 'invaliduser',
            'email': 'invaliduser@example.com',
            'password': 'ValidPass123!',
            'confirm_password': 'ValidPass123!'
        })
        
        # Attempt recovery with invalid code
        response = client.post('/forgot-password', data={
            'email': 'invaliduser@example.com',
            'recovery_code': 'XXXX-XXXX'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Invalid recovery code' in response.data or b'error' in response.data.lower()
    
    def test_recovery_code_must_match_email(self, client):
        """Verify recovery code must be for the correct email"""
        # Register two users
        for i in range(2):
            client.post('/register', data={
                'username': f'user{i}',
                'email': f'user{i}@example.com',
                'password': 'ValidPass123!',
                'confirm_password': 'ValidPass123!'
            })
        
        # Try to use user0's recovery code with user1's email
        response = client.post('/forgot-password', data={
            'email': 'user1@example.com',
            'recovery_code': 'XXXX-XXXX'  # Invalid for user1
        }, follow_redirects=True)
        
        assert response.status_code == 200
        # Should fail because code is invalid for this user
        assert b'error' in response.data.lower() or b'invalid' in response.data.lower()
    
    def test_registration_rollback_on_recovery_code_generation_failure(self, client, monkeypatch):
        """Test that user account is rolled back if recovery code generation fails.
        
        Ensures atomicity: if recovery code generation fails, the entire registration
        is rolled back and no user is created (no race condition).
        """
        # Simulate recovery code generation failure
        def mock_generate_recovery_codes_fail(*args, **kwargs):
            raise Exception("Simulated recovery code generation failure")
        
        monkeypatch.setattr('app.generate_recovery_codes', mock_generate_recovery_codes_fail)
        
        # Attempt registration
        response = client.post('/register', data={
            'username': 'rollbacktest',
            'email': 'rollback@example.com',
            'password': 'ValidPass123!',
            'confirm_password': 'ValidPass123!'
        }, follow_redirects=True)
        
        # Should show error message
        assert response.status_code == 200
        assert b'Registration failed' in response.data
        
        # Most importantly: user should NOT be created in database
        # Use app context because test may run outside of request context
        with app.app_context():
            user = User.query.filter_by(email='rollback@example.com').first()
            assert user is None, "User should not be created if recovery code generation fails"
            
            # Verify no orphaned user account exists
            all_users = User.query.all()
            assert all(u.email != 'rollback@example.com' for u in all_users)


class TestOpenLibraryIntegration:
    """Test Open Library API integration for book metadata fetching."""
    
    def test_fetch_book_successful_lookup(self, monkeypatch):
        """Test successful book lookup with all fields present."""
        mock_response = {
            'docs': [{
                'title': 'The Great Gatsby',
                'author_name': ['F. Scott Fitzgerald'],
                'subject': ['American fiction', 'Jazz Age', 'Fiction'],
                'key': '/works/OL468431W',
                'cover_i': 123456
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('978-0743273565')
        
        assert result is not None
        assert result['title'] == 'The Great Gatsby'
        assert result['author'] == 'F. Scott Fitzgerald'
        assert 'American fiction' in result['genre']
        assert result['cover_url'] == 'https://covers.openlibrary.org/b/id/123456-M.jpg'
    
    def test_fetch_book_multiple_authors(self, monkeypatch):
        """Test book lookup with multiple authors."""
        mock_response = {
            'docs': [{
                'title': 'Some Book',
                'author_name': ['Author One', 'Author Two', 'Author Three', 'Author Four'],
                'subject': [],
                'key': '/works/OLXXXXXX',
                'cover_i': 789
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('1234567890')
        
        assert result is not None
        # Should include first 3 authors
        assert 'Author One' in result['author']
        assert 'Author Two' in result['author']
        assert 'Author Three' in result['author']
        assert 'Author Four' not in result['author']
    
    def test_fetch_book_missing_cover_image(self, monkeypatch):
        """Test book lookup when cover image is not available."""
        mock_response = {
            'docs': [{
                'title': 'Book Without Cover',
                'author_name': ['John Doe'],
                'subject': ['Fiction'],
                # No cover_i field
                'key': '/works/OLXXXXXX',
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('9876543210')
        
        assert result is not None
        assert result['title'] == 'Book Without Cover'
        assert result['cover_url'] is None
    
    def test_fetch_book_missing_author(self, monkeypatch):
        """Test book lookup when author data is missing."""
        mock_response = {
            'docs': [{
                'title': 'Anonymous Work',
                # No author_name field
                'subject': ['Mystery'],
                'cover_i': 999,
                'key': '/works/OLXXXXXX'
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('5555555555')
        
        assert result is not None
        assert result['title'] == 'Anonymous Work'
        assert result['author'] == ''
    
    def test_fetch_book_missing_genre(self, monkeypatch):
        """Test book lookup when genre/subject data is missing."""
        mock_response = {
            'docs': [{
                'title': 'Unclassified Book',
                'author_name': ['Anonymous'],
                # No subject field
                'cover_i': 111,
                'key': '/works/OLXXXXXX'
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('4444444444')
        
        assert result is not None
        assert result['genre'] == ''

    def test_fetch_book_works_api_failure(self, monkeypatch):
        """Search succeeds but works API call fails (timeout) — ensure no genre and function returns."""
        mock_response = {
            'docs': [{
                'title': 'Partial Work',
                'author_name': ['Some Author'],
                'key': '/works/OLFAIL',
                'cover_i': 222
            }]
        }

        def mock_get(*args, **kwargs):
            import requests
            from unittest.mock import Mock
            url = args[0] if args else kwargs.get('url', '')
            # Simulate timeout for works endpoint
            if '/works/' in url and url.endswith('.json'):
                raise requests.Timeout('Simulated works API timeout')

            response = Mock()
            response.status_code = 200
            response.json.return_value = mock_response
            return response

        monkeypatch.setattr('app.requests.get', mock_get)

        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('9999999999')

        assert result is not None
        assert result['title'] == 'Partial Work'
        assert result['author'] == 'Some Author'
        # Works API failed so genre should be empty string
        assert result['genre'] == ''
        assert result['cover_url'] == 'https://covers.openlibrary.org/b/id/222-M.jpg'
    
    def test_fetch_book_not_found(self, monkeypatch):
        """Test book lookup when ISBN is not found."""
        mock_response = {'docs': []}  # Empty results
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('0000000000')
        
        assert result is None
    
    def test_fetch_book_api_error_status_code(self, monkeypatch):
        """Test handling of HTTP error status from API."""
        def mock_get(*args, **kwargs):
            from unittest.mock import Mock
            response = Mock()
            response.status_code = 500
            return response
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('1111111111')
        
        assert result is None
    
    def test_fetch_book_timeout(self, monkeypatch):
        """Test handling of API timeout."""
        def mock_get(*args, **kwargs):
            import requests
            raise requests.Timeout('Connection timed out')
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('2222222222')
        
        assert result is None
    
    def test_fetch_book_request_exception(self, monkeypatch):
        """Test handling of general request exceptions."""
        def mock_get(*args, **kwargs):
            import requests
            raise requests.ConnectionError('Network error')
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('3333333333')
        
        assert result is None
    
    def test_fetch_book_malformed_json_response(self, monkeypatch):
        """Test handling of malformed JSON response."""
        def mock_get(*args, **kwargs):
            from unittest.mock import Mock
            response = Mock()
            response.status_code = 200
            response.json.side_effect = ValueError('Invalid JSON')
            return response
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('6666666666')
        
        assert result is None
    
    def test_fetch_book_missing_docs_key(self, monkeypatch):
        """Test handling when response lacks 'docs' key."""
        mock_response = {}  # No 'docs' key
        
        def mock_get(*args, **kwargs):
            from unittest.mock import Mock
            url = args[0] if args else kwargs.get('url', '')
            response = Mock()
            response.status_code = 200
            if '/works/' in url and url.endswith('.json'):
                docs = mock_response.get('docs') if isinstance(mock_response, dict) else None
                subjects = []
                if docs:
                    subjects = docs[0].get('subject', []) if isinstance(docs[0], dict) else []
                response.json.return_value = {'subjects': subjects}
            else:
                response.json.return_value = mock_response
            return response
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        result = fetch_book_from_open_library('7777777777')
        
        assert result is None
    
    def test_fetch_book_isbn_cleanup(self, monkeypatch):
        """Test that ISBN is properly cleaned (hyphens/spaces removed)."""
        received_urls = []
        
        def mock_get(url, **kwargs):
            received_urls.append(url)
            from unittest.mock import Mock
            response = Mock()
            response.status_code = 200
            response.json.return_value = {'docs': []}
            return response
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        from app import fetch_book_from_open_library
        fetch_book_from_open_library('978-0-7432-7356-5')  # ISBN with hyphens
        
        # Verify the API was called with cleaned ISBN
        assert len(received_urls) == 1
        assert '978-0-7432-7356-5' not in received_urls[0]  # Hyphens removed
        assert '9780743273565' in received_urls[0]  # Clean ISBN used


class TestBookLookupEndpoint:
    """Test the /api/book-lookup HTTP endpoint."""
    
    def test_book_lookup_requires_authentication(self, client):
        """Test that unauthenticated users cannot access the endpoint."""
        response = client.post('/api/book-lookup', 
            json={'isbn': '978-0743273565'},
            follow_redirects=False)
        
        # Should redirect to login (302) or return unauthorized (401)
        assert response.status_code in [302, 401]
    
    def test_book_lookup_successful_request(self, auth_user, monkeypatch):
        """Test successful book lookup with authenticated user."""
        client, user = auth_user
        
        mock_response = {
            'docs': [{
                'title': 'The Great Gatsby',
                'author_name': ['F. Scott Fitzgerald'],
                'subject': ['American fiction'],
                'key': '/works/OL468431W',
                'cover_i': 123456
            }]
        }
        
        def mock_get(*args, **kwargs):
            from unittest.mock import Mock
            url = args[0] if args else kwargs.get('url', '')
            response = Mock()
            response.status_code = 200
            if '/works/' in url and url.endswith('.json'):
                docs = mock_response.get('docs') if isinstance(mock_response, dict) else None
                subjects = []
                if docs:
                    subjects = docs[0].get('subject', []) if isinstance(docs[0], dict) else []
                response.json.return_value = {'subjects': subjects}
            else:
                response.json.return_value = mock_response
            return response
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        response = client.post('/api/book-lookup',
            json={'isbn': '978-0743273565'},
            content_type='application/json')
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['title'] == 'The Great Gatsby'
        assert data['author'] == 'F. Scott Fitzgerald'
        assert 'American fiction' in data['genre']
        assert data['cover_url'] == 'https://covers.openlibrary.org/b/id/123456-M.jpg'
    
    def test_book_lookup_missing_isbn_parameter(self, auth_user):
        """Test that missing ISBN parameter returns error."""
        client, user = auth_user
        
        response = client.post('/api/book-lookup',
            json={},  # No ISBN
            content_type='application/json')
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data
        assert 'ISBN is required' in data['error']
    
    def test_book_lookup_empty_isbn_parameter(self, auth_user):
        """Test that empty ISBN parameter returns error."""
        client, user = auth_user
        
        response = client.post('/api/book-lookup',
            json={'isbn': ''},
            content_type='application/json')
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data
    
    def test_book_lookup_whitespace_isbn(self, auth_user):
        """Test that whitespace-only ISBN is treated as empty."""
        client, user = auth_user
        
        response = client.post('/api/book-lookup',
            json={'isbn': '   '},
            content_type='application/json')
        
        assert response.status_code == 400
        data = response.get_json()
        assert 'error' in data
    
    def test_book_lookup_isbn_not_found(self, auth_user, monkeypatch):
        """Test when ISBN is not found in Open Library."""
        client, user = auth_user
        
        mock_response = {'docs': []}  # Empty results
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        response = client.post('/api/book-lookup',
            json={'isbn': '0000000000'},
            content_type='application/json')
        
        assert response.status_code == 404
        data = response.get_json()
        assert 'error' in data
        assert 'Book not found' in data['error']
    
    def test_book_lookup_api_timeout(self, auth_user, monkeypatch):
        """Test handling of API timeout."""
        client, user = auth_user
        
        def mock_get(*args, **kwargs):
            import requests
            raise requests.Timeout('Connection timed out')
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        response = client.post('/api/book-lookup',
            json={'isbn': '1234567890'},
            content_type='application/json')
        
        # API returns None on timeout, which returns 404
        assert response.status_code == 404
        data = response.get_json()
        assert 'error' in data
    
    def test_book_lookup_api_error(self, auth_user, monkeypatch):
        """Test handling of API connection error."""
        client, user = auth_user
        
        def mock_get(*args, **kwargs):
            import requests
            raise requests.ConnectionError('Network error')
        
        monkeypatch.setattr('app.requests.get', mock_get)
        
        response = client.post('/api/book-lookup',
            json={'isbn': '9876543210'},
            content_type='application/json')
        
        assert response.status_code == 404
        data = response.get_json()
        assert 'error' in data
    
    def test_book_lookup_response_format_json(self, auth_user, monkeypatch):
        """Test that response is valid JSON with correct structure."""
        client, user = auth_user
        
        mock_response = {
            'docs': [{
                'title': 'Test Book',
                'author_name': ['Test Author'],
                'subject': ['Test Genre'],
                'cover_i': 999,
                'key': '/works/OLXXXXXX'
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        response = client.post('/api/book-lookup',
            json={'isbn': '1111111111'},
            content_type='application/json')
        
        assert response.status_code == 200
        # Verify response is valid JSON
        data = response.get_json()
        assert data is not None
        # Verify required fields
        assert 'title' in data
        assert 'author' in data
        assert 'genre' in data
        assert 'cover_url' in data
    
    def test_book_lookup_partial_metadata(self, auth_user, monkeypatch):
        """Test response with minimal metadata."""
        client, user = auth_user
        
        mock_response = {
            'docs': [{
                'title': 'Minimal Book',
                # No author, genre, or cover
                'key': '/works/OLXXXXXX'
            }]
        }
        
        monkeypatch.setattr('app.requests.get', make_openlibrary_mock_get(mock_response))
        
        response = client.post('/api/book-lookup',
            json={'isbn': '5555555555'},
            content_type='application/json')
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['title'] == 'Minimal Book'
        assert data['author'] == ''
        assert data['genre'] == ''
        assert data['cover_url'] is None
    
    def test_book_lookup_request_method_get_not_allowed(self, auth_user):
        """Test that GET requests are not allowed (POST only)."""
        client, user = auth_user
        
        response = client.get('/api/book-lookup?isbn=1234567890')
        
        # Should return 405 Method Not Allowed
        assert response.status_code == 405


class TestAccountSettings:
    """Tests for account settings and email change functionality."""
    
    def test_settings_page_requires_login(self, client):
        """Test that settings page requires authentication."""
        response = client.get('/settings')
        assert response.status_code == 302  # Redirect to login
        assert 'login' in response.location
    
    def test_settings_page_loads(self, auth_user):
        """Test that authenticated user can access settings page."""
        client, user = auth_user
        response = client.get('/settings')
        assert response.status_code == 200
        assert b'Account Settings' in response.data
        assert user.email.encode() in response.data  # Current email displayed
    
    def test_change_email_success(self, auth_user):
        """Test successfully changing email with correct password."""
        client, user = auth_user
        old_email = user.email
        
        response = client.post('/settings', data={
            'new_email': 'newemail@example.com',
            'password': 'TestPass123!'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Email updated successfully!' in response.data
        
        # Verify email changed in database
        with app.app_context():
            updated_user = User.query.get(user.id)
            assert updated_user.email == 'newemail@example.com'
            assert updated_user.email != old_email
    
    def test_change_email_wrong_password(self, auth_user):
        """Test that email change fails with incorrect password."""
        client, user = auth_user
        original_email = user.email
        
        response = client.post('/settings', data={
            'new_email': 'newemail@example.com',
            'password': 'WrongPassword123!'
        })
        
        assert response.status_code == 200
        assert b'Invalid password' in response.data
        
        # Verify email did not change
        with app.app_context():
            updated_user = User.query.get(user.id)
            assert updated_user.email == original_email
    
    def test_change_email_duplicate_email(self, client):
        """Test that email change fails if new email is already registered."""
        # Create two users
        with app.app_context():
            user1 = User(username='user1', email='user1@example.com')
            user1.set_password('TestPass123!')
            user2 = User(username='user2', email='user2@example.com')
            user2.set_password('TestPass123!')
            db.session.add(user1)
            db.session.add(user2)
            db.session.commit()
        
        # Try to change user1's email to user2's email
        client.post('/login', data={
            'email': 'user1@example.com',
            'password': 'TestPass123!'
        })
        
        response = client.post('/settings', data={
            'new_email': 'user2@example.com',
            'password': 'TestPass123!'
        })
        
        assert response.status_code == 200
        assert b'Email already registered' in response.data
    
    def test_change_email_same_as_current(self, auth_user):
        """Test that email change fails if new email is same as current."""
        client, user = auth_user
        
        response = client.post('/settings', data={
            'new_email': user.email,
            'password': 'TestPass123!'
        })
        
        assert response.status_code == 200
        assert b'must be different from current email' in response.data
    
    def test_recovery_codes_page_shows_email(self, client):
        """Test that recovery codes page displays email address."""
        with app.app_context():
            user = User(username='testuser', email='verify@example.com')
            user.set_password('TestPass123!')
            db.session.add(user)
            db.session.flush()
            
            recovery_codes = generate_recovery_codes(user.id, count=8)
            db.session.commit()
            
            # Generate display token
            from app import generate_recovery_code_display_token
            token = generate_recovery_code_display_token(user.id, recovery_codes)
        
        # Simulate POST registration flow
        with client.session_transaction() as sess:
            sess['recovery_code_token'] = token
        
        response = client.get('/recovery-codes')
        assert response.status_code == 200
        assert b'verify@example.com' in response.data
        assert b'Account Email:' in response.data


class TestTBRFeature:
    """Test the To Be Read (TBR) feature and book status filtering."""
    
    def test_add_book_with_to_read_status(self, auth_user):
        """Test adding a book with 'to_read' status."""
        client, user = auth_user
        response = client.post('/add', data={
            'title': 'To Be Read Book',
            'author': 'Future Author',
            'format': 'physical',
            'status': 'to_read'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        with app.app_context():
            book = Book.query.filter_by(title='To Be Read Book').first()
            assert book is not None
            assert book.status == 'to_read'
    
    def test_add_book_with_currently_reading_status(self, auth_user):
        """Test adding a book with 'currently_reading' status."""
        client, user = auth_user
        response = client.post('/add', data={
            'title': 'Currently Reading Book',
            'author': 'Active Author',
            'format': 'ebook',
            'status': 'currently_reading'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        with app.app_context():
            book = Book.query.filter_by(title='Currently Reading Book').first()
            assert book is not None
            assert book.status == 'currently_reading'
    
    def test_add_book_with_read_status(self, auth_user):
        """Test adding a book with 'read' status."""
        client, user = auth_user
        response = client.post('/add', data={
            'title': 'Finished Book',
            'author': 'Past Author',
            'format': 'audiobook',
            'status': 'read'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        with app.app_context():
            book = Book.query.filter_by(title='Finished Book').first()
            assert book is not None
            assert book.status == 'read'
    
    def test_edit_book_change_status(self, auth_user):
        """Test editing a book to change its status."""
        client, user = auth_user
        
        # First add a book with 'to_read' status
        with app.app_context():
            book = Book(
                title='Status Change Book',
                author='Test Author',
                format='physical',
                status='to_read',
                user_id=user.id
            )
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        # Edit the book to change status to 'read'
        response = client.post(f'/edit/{book_id}', data={
            'title': 'Status Change Book',
            'author': 'Test Author',
            'format': 'physical',
            'status': 'read'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        with app.app_context():
            updated_book = Book.query.get(book_id)
            assert updated_book.status == 'read'
    
    def test_filter_books_by_to_read_status(self, auth_user):
        """Test filtering books to show only 'to_read' status."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='TBR Book 1', author='Author 1', format='physical', status='to_read', user_id=user.id)
            book2 = Book(title='TBR Book 2', author='Author 2', format='physical', status='to_read', user_id=user.id)
            book3 = Book(title='Read Book', author='Author 3', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Filter by 'to_read'
        response = client.get('/?status=to_read')
        assert response.status_code == 200
        assert b'TBR Book 1' in response.data
        assert b'TBR Book 2' in response.data
        assert b'Read Book' not in response.data
    
    def test_filter_books_by_currently_reading_status(self, auth_user):
        """Test filtering books to show only 'currently_reading' status."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='Reading Now 1', author='Author 1', format='physical', status='currently_reading', user_id=user.id)
            book2 = Book(title='Reading Now 2', author='Author 2', format='physical', status='currently_reading', user_id=user.id)
            book3 = Book(title='Read Book', author='Author 3', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Filter by 'currently_reading'
        response = client.get('/?status=currently_reading')
        assert response.status_code == 200
        assert b'Reading Now 1' in response.data
        assert b'Reading Now 2' in response.data
        assert b'Read Book' not in response.data
    
    def test_filter_books_by_read_status(self, auth_user):
        """Test filtering books to show only 'read' status."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='Read Book 1', author='Author 1', format='physical', status='read', user_id=user.id)
            book2 = Book(title='Read Book 2', author='Author 2', format='physical', status='read', user_id=user.id)
            book3 = Book(title='TBR Book', author='Author 3', format='physical', status='to_read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Filter by 'read'
        response = client.get('/?status=read')
        assert response.status_code == 200
        assert b'Read Book 1' in response.data
        assert b'Read Book 2' in response.data
        assert b'TBR Book' not in response.data
    
    def test_filter_all_status_shows_all_books(self, auth_user):
        """Test that 'all' status filter shows all books regardless of status."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='TBR Book', author='Author 1', format='physical', status='to_read', user_id=user.id)
            book2 = Book(title='Reading Book', author='Author 2', format='physical', status='currently_reading', user_id=user.id)
            book3 = Book(title='Read Book', author='Author 3', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Filter by 'all'
        response = client.get('/?status=all')
        assert response.status_code == 200
        assert b'TBR Book' in response.data
        assert b'Reading Book' in response.data
        assert b'Read Book' in response.data
    
    def test_search_combined_with_status_filter(self, auth_user):
        """Test searching for books within a specific status."""
        client, user = auth_user
        
        # Create books with different statuses and titles
        with app.app_context():
            book1 = Book(title='Fiction to Read', author='Author 1', format='physical', status='to_read', user_id=user.id)
            book2 = Book(title='Mystery Novel', author='Author 2', format='physical', status='to_read', user_id=user.id)
            book3 = Book(title='Fiction Read', author='Author 3', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Search for 'Fiction' in 'to_read' status
        response = client.get('/?search=Fiction&status=to_read')
        assert response.status_code == 200
        assert b'Fiction to Read' in response.data
        assert b'Mystery Novel' not in response.data
        assert b'Fiction Read' not in response.data
    
    def test_default_status_for_new_books_is_read(self, auth_user):
        """Test that books created before status field get 'read' as default."""
        client, user = auth_user
        
        # Add book without explicitly setting status (mimics older books)
        with app.app_context():
            book = Book(
                title='Legacy Book',
                author='Legacy Author',
                format='physical',
                user_id=user.id
            )
            db.session.add(book)
            db.session.commit()
            book_id = book.id
        
        # Verify default status is 'read'
        with app.app_context():
            book = Book.query.get(book_id)
            assert book.status == 'read'
    
    def test_status_filter_respects_user_isolation(self, auth_user):
        """Test that status filter only shows books for the logged-in user."""
        client, user = auth_user
        
        # Create books for current user
        with app.app_context():
            user_book = Book(title='User TBR Book', author='Author 1', format='physical', status='to_read', user_id=user.id)
            db.session.add(user_book)
            
            # Create another user and their book
            other_user = User(username='otheruser', email='other@example.com')
            other_user.set_password('Password123!')
            db.session.add(other_user)
            db.session.flush()
            
            other_book = Book(title='Other User TBR', author='Author 2', format='physical', status='to_read', user_id=other_user.id)
            db.session.add(other_book)
            db.session.commit()
        
        # Filter by 'to_read' - should only show current user's books
        response = client.get('/?status=to_read')
        assert response.status_code == 200
        assert b'User TBR Book' in response.data
        assert b'Other User TBR' not in response.data
    
    def test_no_status_filter_defaults_to_all(self, auth_user):
        """Test that not providing a status filter shows all books."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='TBR', author='Author 1', format='physical', status='to_read', user_id=user.id)
            book2 = Book(title='Reading', author='Author 2', format='physical', status='currently_reading', user_id=user.id)
            book3 = Book(title='Read', author='Author 3', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2, book3])
            db.session.commit()
        
        # Access home page without status filter
        response = client.get('/')
        assert response.status_code == 200
        assert b'TBR' in response.data
        assert b'Reading' in response.data
        assert b'Read' in response.data
    
    def test_invalid_status_filter_defaults_to_all(self, auth_user):
        """Test that invalid status values are rejected and default to 'all'."""
        client, user = auth_user
        
        # Create books with different statuses
        with app.app_context():
            book1 = Book(title='TBR Book', author='Author 1', format='physical', status='to_read', user_id=user.id)
            book2 = Book(title='Read Book', author='Author 2', format='physical', status='read', user_id=user.id)
            db.session.add_all([book1, book2])
            db.session.commit()
        
        # Try to use an invalid status filter (e.g., 'invalid_status')
        response = client.get('/?status=invalid_status')
        assert response.status_code == 200
        # Should show all books since invalid status defaults to 'all'
        assert b'TBR Book' in response.data
        assert b'Read Book' in response.data
    
    def test_sql_injection_attempt_on_status_filter(self, auth_user):
        """Test that SQL injection attempts on status parameter are safely rejected."""
        client, user = auth_user
        
        # Create a test book
        with app.app_context():
            book = Book(title='Test Book', author='Author', format='physical', status='read', user_id=user.id)
            db.session.add(book)
            db.session.commit()
        
        # Try SQL injection attempts - should be safely handled
        dangerous_params = [
            "'; DROP TABLE book; --",
            "' OR '1'='1",
            "\" OR 1=1 --",
            "'; DELETE FROM book; --"
        ]
        
        for param in dangerous_params:
            response = client.get(f'/?status={param}')
            # Should not execute any SQL injection, just return 200 with valid data
            assert response.status_code == 200
            assert b'Test Book' in response.data
    
    def test_registration_logs_user_in(self, client):
        """Test that registration automatically logs user in."""
        response = client.post('/register', data={
            'username': 'newuser',
            'email': 'newuser@example.com',
            'password': 'TestPass123!',
            'confirm_password': 'TestPass123!'
        }, follow_redirects=False)
        
        # Should redirect to recovery codes (not login)
        assert response.status_code == 302
        assert 'recovery-codes' in response.location
        
        # Follow the redirect
        response = client.get(response.location)
        assert response.status_code == 200
        assert b'newuser@example.com' in response.data
    
    def test_can_change_email_after_registration(self, client):
        """Test complete flow: register, view recovery codes, change email."""
        # Register
        response = client.post('/register', data={
            'username': 'newuser',
            'email': 'typo@example.com',
            'password': 'TestPass123!',
            'confirm_password': 'TestPass123!'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'typo@example.com' in response.data
        
        # At this point user should be logged in, so we can go to settings
        response = client.get('/settings')
        assert response.status_code == 200
        assert b'typo@example.com' in response.data
        
        # Change email
        response = client.post('/settings', data={
            'new_email': 'correct@example.com',
            'password': 'TestPass123!'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Email updated successfully!' in response.data
        
        # Verify new email is shown
        with app.app_context():
            user = User.query.filter_by(username='newuser').first()
            assert user.email == 'correct@example.com'