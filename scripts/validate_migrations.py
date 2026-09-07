from pathlib import Path

MIGRATIONS = Path('migrations')


def main() -> None:
    files = sorted(MIGRATIONS.glob('*.sql'))
    assert files, 'no_migrations_found'
    for path in files:
        text = path.read_text(encoding='utf-8').lower()
        if path.name.endswith('.down.sql'):
            assert 'drop table' in text or 'alter table' in text, f'invalid_rollback:{path.name}'
            continue
        assert 'create table' in text or 'alter table' in text, f'invalid_migration:{path.name}'
        rollback = path.with_name(path.name.removesuffix('.sql') + '.down.sql')
        assert rollback.is_file(), f'rollback_missing:{path.name}'
    print(f'validated {len(files)} migration files')


if __name__ == '__main__':
    main()
