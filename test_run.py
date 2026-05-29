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

print("=== 照合結果 ===")
print(f"CSV件数:   {result['total_csv']}件")
print(f"DB件数:    {result['total_db']}件")
print(f"一致:      {len(result['matched'])}件")
print(f"価格変更:  {len(result['price_changed'])}件")
print(f"成約候補:  {len(result['not_in_db'])}件")
print(f"成約確定:  {len(result['confirmed_sold'])}件")

if result['price_changed']:
    print("\n--- 価格変更（差が大きい順）---")
    sorted_price = sorted(
        result['price_changed'],
        key=lambda p: abs((mod.parse_price(p.get('csv_価格','')) or 0) - (mod.parse_price(p.get('db_価格','')) or 0)),
        reverse=True
    )
    for p in sorted_price[:10]:
        db_p  = mod.parse_price(p.get('db_価格',''))
        csv_p = mod.parse_price(p.get('csv_価格',''))
        diff  = abs((csv_p or 0) - (db_p or 0))
        print(f"  差:{diff:.0f}万 | {p.get('建物名','')[:20]} | {p.get('所在地','')[:20]}")

print("\n--- 成約候補（最初の10件）---")
for p in result['not_in_db'][:10]:
    print(f"  {p['建物名'][:25]}  {p['所在地'][:20]}  {p['価格']}")

print("\n--- 一致例（最初の3件）---")
for p in result['matched'][:3]:
    print(f"  {p.get('建物名','')[:25]}  {p.get('所在地','')[:20]}")
