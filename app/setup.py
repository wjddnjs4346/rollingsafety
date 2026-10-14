import getpass
from .core import storage, initialize, connect, password_hash
from .backup import acquire_server_lock

def main():
    path=storage()
    handle=acquire_server_lock(path)
    initialize(path)
    with connect(path) as db:
        if db.execute("SELECT 1 FROM users WHERE role='owner'").fetchone():
            raise SystemExit('Owner already exists. Setup will not overwrite accounts.')
        username=input('Owner username: ').strip()
        password=getpass.getpass('Password (12+ characters): ')
        confirmation=getpass.getpass('Confirm password: ')
        if not username or len(username)>100 or len(password)<12 or len(password)>256 or password!=confirmation:
            raise SystemExit('Invalid username/password or confirmation mismatch.')
        db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(username,password_hash(password),'owner'))
    print('Owner created. No credentials were written to a configuration file.')

if __name__=='__main__': main()
