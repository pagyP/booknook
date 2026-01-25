import pytest
from app import app, db, Book, User


def test_genre_splitting_and_filter():
    """Verify comma-separated genres are split, deduplicated and filterable.

    This test creates three books for the logged-in user with compound
    comma-separated genre fields and checks that the index page shows
    individual genres and that filtering by a single genre returns
    the expected books.
    """
    # Prepare clean test database and create a user
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()

        user = User(username='testuser', email='test@example.com')
        user.set_password('TestPass123!')
        db.session.add(user)
        db.session.commit()

        # Create books with compound genres
        b1 = Book(title='Book A', author='Author A', genre='Fiction, Mystery', user_id=user.id)
        b2 = Book(title='Book B', author='Author B', genre='Mystery, Thriller', user_id=user.id)
        b3 = Book(title='Book C', author='Author C', genre='Nonfiction', user_id=user.id)
        db.session.add_all([b1, b2, b3])
        db.session.commit()

    # Ensure test config matches other tests (disable rate limiter / CSRF)
    app.config['TESTING'] = True
    app.config['RATELIMIT_ENABLED'] = False
    app.config['WTF_CSRF_ENABLED'] = False

    client = app.test_client()
    # Log in using the test client so index is accessible
    rv_login = client.post('/login', data={'email': 'test@example.com', 'password': 'TestPass123!'}, follow_redirects=True)
    assert rv_login.status_code == 200

    # Index page should include individual genre options
    rv = client.get('/')
    assert rv.status_code == 200
    html = rv.get_data(as_text=True)
    assert 'Fiction' in html
    assert 'Mystery' in html
    assert 'Thriller' in html
    assert 'Nonfiction' in html

    # Filtering by Fiction should return only Book A
    rv_fiction = client.get('/?genre=Fiction')
    html_fiction = rv_fiction.get_data(as_text=True)
    assert 'Book A' in html_fiction
    assert 'Book B' not in html_fiction

    # Filtering by Mystery should return both Book A and Book B
    rv_mystery = client.get('/?genre=Mystery')
    html_mystery = rv_mystery.get_data(as_text=True)
    assert 'Book A' in html_mystery
    assert 'Book B' in html_mystery
