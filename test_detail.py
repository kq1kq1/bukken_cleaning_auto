import sys, io, types
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
for m in ['tkinter','tkinter.filedialog','tkinter.messagebox']:
    sys.modules[m] = types.ModuleType(m)

import importlib.util, logging
logging.disable(logging.WARNING)
spec = importlib.util.spec_from_file_location('check_csv', 'check_csv.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
logging.disable(logging.NOTSET)

csv_path = r'C:\Users\user\Downloads\BukkenIchiran (1).csv'
cfg = mod.load_config()
result = mod.compare(csv_path, cfg)

print("=== 価格変更（詳細 差が大きい順）===")
sorted_price = sorted(
    result['price_changed'],
    key=lambda p: abs((mod.parse_price(p.get('csv_価格','')) or 0) - (mod.parse_price(p.get('db_価格','')) or 0)),
    reverse=True
)
for p in sorted_price[:20]:
    db_p  = mod.parse_price(p.get('db_価格',''))
    csv_p = mod.parse_price(p.get('csv_価格',''))
    diff  = abs((csv_p or 0) - (db_p or 0))
    name = p.get('建物名','')[:25]
    addr = p.get('所在地','')[:25]
    print(f"  差:{diff:5.0f}万 | DB:{db_p:.0f}万 -> CSV:{csv_p:.0f}万 | {name:25} | {addr}")

print()
print("=== 成約候補（全件）===")
for p in result['not_in_db']:
    print(f"  {p['建物名'][:25]:25} | {p['所在地'][:25]:25} | {p['価格']}")

print()
print(f"合計: 一致={len(result['matched'])} 価格変更={len(result['price_changed'])} "
      f"成約候補={len(result['not_in_db'])} 成約確定={len(result['confirmed_sold'])}")
