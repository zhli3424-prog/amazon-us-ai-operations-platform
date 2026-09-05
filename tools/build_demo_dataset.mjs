import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(root, "outputs", "01a0613c-6ac5-71a2-a071-fca4edb9f5ff");
const previewDir = path.join(outputDir, "previews");

const productHeaders = ["sku", "asin", "source_title", "category", "price", "cost", "monthly_sales", "rating", "review_count", "competition_score", "stock_units", "daily_sales", "lead_time_days"];
const productSheetHeaders = ["商品编码", "模拟商品编码", "商品名称", "品类", "售价（美元）", "成本（美元）", "近30天销量", "评分", "评价数", "竞争度", "当前库存", "近7天日均销量", "补货周期（天）", "预估月销售额", "毛利率", "运营提示"];
const products = [
  ["KT-SCALE-5K-US", "B0D7K2M4Q1", "炉边优选 不锈钢电子厨房秤，11磅量程", "厨房秤", 22.99, 7.85, 920, 4.5, 684, 48, 545, 29.8, 14],
  ["KT-FROTH-01-US", "B0C9R6T2L8", "松沫 三档充电式奶泡器", "奶泡器", 18.49, 5.62, 1380, 4.3, 1247, 72, 1260, 44.2, 12],
  ["KT-SEAL-12-US", "B0D3V8N5S2", "炉边优选 小型真空封口机，附12个密封袋", "真空封口机", 49.99, 19.4, 410, 4.2, 219, 39, 45, 13.6, 24],
  ["HM-VAC-02-US", "B0D8H4P7X3", "北境家居 无线手持吸尘器，附2个滤芯", "手持吸尘器", 42.99, 17.65, 655, 4.1, 392, 58, 38, 21.9, 28],
  ["HM-LIGHT-6-US", "B0C6W9J3F5", "北境家居 人体感应橱柜灯，暖白光6只装", "橱柜灯", 34.99, 12.8, 1110, 4.4, 1806, 76, 1125, 36.5, 18],
  ["HM-BAG-10-US", "B0D2Q7A6E9", "清巢 加厚真空收纳袋10只装，附手泵", "真空收纳袋", 27.99, 8.95, 780, 4.3, 963, 64, 620, 25.7, 20],
  ["PET-ROLL-01-US", "B0C8M5U2K7", "爪印家 可重复使用宠物粘毛滚筒", "宠物粘毛器", 19.99, 5.35, 1720, 4.6, 3214, 81, 1550, 55.4, 16],
  ["PET-FILT-12-US", "B0D6B3R9T4", "爪印家 猫咪饮水机替换滤芯12片装", "饮水机滤芯", 25.49, 7.7, 540, 4.4, 577, 43, 88, 18.1, 22],
  ["PET-BOWL-S-US", "B0D1L8Y5C6", "爪印家 防滑慢食狗碗，小号", "慢食碗", 16.99, 4.92, 465, 4.2, 348, 51, 455, 14.9, 18],
  ["TR-CUBE-6-US", "B0C7P4N8D2", "漫旅 可扩容旅行收纳袋6件套", "旅行收纳袋", 31.99, 10.75, 890, 4.5, 1388, 69, 880, 31.7, 26],
  ["TR-WASH-L-US", "B0D9S2G5V7", "漫旅 大容量悬挂洗漱包，带干湿分区", "洗漱包", 26.99, 8.6, 335, 4.4, 176, 35, 55, 10.8, 24],
  ["TR-LUG-50-US", "B0C5E8Q1M4", "漫旅 电子行李秤，110磅量程", "行李秤", 15.99, 4.25, 1240, 4.5, 2456, 84, 920, 42.5, 15],
  ["FT-BAND-5-US", "B0D4J7F2A8", "流光健身 五档阻力带套装，附收纳袋", "阻力带", 23.99, 6.95, 760, 4.3, 902, 74, 710, 24.6, 20],
  ["FT-STRAP-2-US", "B0C3T6K9H1", "流光健身 可调节瑜伽垫背带2条装", "瑜伽配件", 14.99, 3.8, 290, 4.5, 144, 28, 190, 8.7, 16],
  ["FT-BALL-SET-US", "B0D5N1R8W3", "流光健身 深层按摩球套装，附收纳袋", "按摩球", 17.99, 4.7, 520, 4.4, 428, 46, 22, 16.8, 19],
  ["OF-STAND-AL-US", "B0C4X7B2P9", "桌境 可折叠铝合金笔记本支架", "笔记本支架", 38.99, 14.9, 610, 4.6, 1088, 63, 680, 20.4, 25],
  ["OF-CABLE-16-US", "B0D2M6C8L5", "桌境 硅胶理线夹16只装", "理线器", 13.49, 3.25, 845, 4.3, 691, 79, 260, 27.3, 14],
  ["OF-MAT-XL-US", "B0D8Q1S4Y6", "桌境 双面素皮桌垫，35×17英寸", "桌垫", 29.99, 9.8, 380, 4.4, 257, 41, 390, 11.6, 23],
  ["OD-LANT-2-US", "B0C9V3D7N2", "溪野 可充电露营灯2只装", "露营灯", 36.99, 13.6, 470, 4.2, 534, 57, 30, 18.9, 30],
  ["OD-DRY-20-US", "B0D7A5K2R8", "溪野 20升防水袋，附手机防水袋", "防水袋", 28.99, 9.25, 315, 4.5, 204, 33, 420, 9.4, 27],
];

const inventoryHeaders = ["sku", "stock_units", "daily_sales", "lead_time_days"];
const inventorySheetHeaders = ["商品编码", "当前库存", "近7天日均销量", "补货周期（天）", "可售天数", "风险等级", "建议补货量"];
const inventory = [
  ["KT-SCALE-5K-US", 520, 30.7, 14], ["KT-FROTH-01-US", 1300, 46.0, 12], ["KT-SEAL-12-US", 39, 13.2, 24],
  ["HM-VAC-02-US", 31, 22.4, 28], ["HM-LIGHT-6-US", 1100, 37.8, 18], ["HM-BAG-10-US", 650, 26.3, 20],
  ["PET-ROLL-01-US", 1600, 57.1, 16], ["PET-FILT-12-US", 79, 17.6, 22], ["PET-BOWL-S-US", 480, 15.5, 18],
  ["TR-CUBE-6-US", 900, 29.4, 26], ["TR-WASH-L-US", 48, 11.2, 24], ["TR-LUG-50-US", 900, 41.7, 15],
  ["FT-BAND-5-US", 690, 25.1, 20], ["FT-STRAP-2-US", 200, 9.3, 16], ["FT-BALL-SET-US", 17, 17.4, 19],
  ["OF-STAND-AL-US", 720, 19.8, 25], ["OF-CABLE-16-US", 248, 28.1, 14], ["OF-MAT-XL-US", 410, 12.1, 23],
  ["OD-LANT-2-US", 24, 16.3, 30], ["OD-DRY-20-US", 400, 10.2, 27],
];

const ticketHeaders = ["sku", "rating", "refund_requested", "title", "message"];
const ticketSheetHeaders = ["商品编码", "评分", "申请退款", "问题标题", "客户反馈", "优先级", "问题类型", "处理时限"];
const tickets = [
  ["HM-VAC-02-US", 1, true, "使用一周后无法充电", "吸尘器刚开始很好用，但现在完全充不进电。我试了两个插座，也清洁了充电触点。如果不能尽快解决，希望办理退款。"],
  ["KT-SCALE-5K-US", 2, false, "重复称重读数会漂移", "同一件物品连续称两次会相差几克。台面是水平的，我也更换了新电池。"],
  ["KT-FROTH-01-US", 4, false, "奶泡细腻，但按键需要适应", "打出的奶泡很细腻，续航也不错，不过第一次使用时不太容易理解如何切换档位。"],
  ["KT-SEAL-12-US", 2, true, "冷冻后密封口开了", "两个袋子放进冰箱一晚后都漏气了。我按说明操作，也在食物上方留足了空间。"],
  ["HM-LIGHT-6-US", 3, false, "人体感应过于灵敏", "灯光够亮，安装也简单，但有人从走廊经过时就会亮，不只是靠近橱柜才触发。"],
  ["HM-BAG-10-US", 1, true, "包装内缺少阀门盖", "其中一个大号收纳袋没有阀门盖，整套无法完整使用。请告知可以补发还是退款。"],
  ["PET-ROLL-01-US", 5, false, "终于能把沙发上的猫毛清干净", "比一次性粘毛纸清理得更干净，集毛仓也容易倒。我已经连续用了三周。"],
  ["PET-FILT-12-US", 2, false, "尺寸不适配我的饮水机", "滤芯看起来和商品图很像，但比我的饮水机托盘宽了几毫米，放不进去。"],
  ["PET-BOWL-S-US", 4, false, "确实有用，但比预想的小", "小狗现在吃得慢多了。标注尺寸没错，不过实际盛粮区域比常见的小号碗更小。"],
  ["TR-CUBE-6-US", 1, true, "第一次旅行拉链就脱开", "第一次旅行装衬衫时压缩拉链就脱开了，收纳袋并没有塞满。我下周出发前需要换货。"],
  ["TR-WASH-L-US", 5, false, "能装下一周的洗漱用品", "干湿分区把少量渗漏控制住了，挂钩在酒店浴室里使用也很牢固。"],
  ["TR-LUG-50-US", 3, false, "称重准确，但读数时间太短", "读数和机场秤一致，不过放下沉重行李箱之前屏幕就熄灭了，很难及时看清。"],
  ["FT-BAND-5-US", 2, true, "最轻档阻力带断裂", "正常热身时最轻的一条在手柄附近断了。虽然没有人受伤，但我不敢继续使用这套产品。"],
  ["FT-STRAP-2-US", 4, false, "简单而且固定牢靠", "两端套环可以牢牢固定瑜伽垫，短途步行时背带也不会勒肩膀。"],
  ["OF-STAND-AL-US", 3, false, "桌面很稳，但转轴太紧", "支架可以稳稳托住我的14英寸笔记本，但转轴非常紧，开会时调节角度需要两只手。"],
  ["OF-CABLE-16-US", 1, true, "胶贴一夜后脱落", "我用酒精清洁桌面并等到完全干燥，但第二天早上还是有好几个理线夹脱落了。"],
  ["OD-LANT-2-US", 2, false, "其中一盏灯明显更暗", "两盏灯都充满了电，但其中一盏在所有档位都明显更暗，另一盏工作正常。"],
  ["OD-DRY-20-US", 5, false, "皮划艇途中衣物保持干燥", "经历多次水花和一阵小雨后，袋里的毛巾和替换衣物仍然是干的。"],
];

function csvEscape(value) {
  const text = typeof value === "boolean" ? String(value).toLowerCase() : String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

async function saveCsv(filename, headers, rows) {
  const content = [headers, ...rows].map(row => row.map(csvEscape).join(",")).join("\r\n") + "\r\n";
  await fs.writeFile(path.join(root, "samples", filename), content, "utf8");
}

function styleSheet(sheet, range, widths) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
  range.format.font = { name: "Aptos", size: 10, color: "#172033" };
  range.format.rowHeight = 20;
  const header = range.getRow(0);
  header.format = { fill: "#16324F", font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" }, rowHeight: 28, verticalAlignment: "center", wrapText: true };
  widths.forEach(([column, width]) => { sheet.getRange(`${column}:${column}`).format.columnWidth = width; });
}

await fs.mkdir(previewDir, { recursive: true });
await saveCsv("products.csv", productHeaders, products);
await saveCsv("inventory.csv", inventoryHeaders, inventory);
await saveCsv("tickets.csv", ticketHeaders, tickets);

const workbook = Workbook.create();
const readme = workbook.worksheets.add("数据说明");
const productSheet = workbook.worksheets.add("商品主数据");
const inventorySheet = workbook.worksheets.add("库存快照");
const ticketSheet = workbook.worksheets.add("客服工单");

readme.showGridLines = false;
readme.getRange("A1").values = [["亚马逊美国站跨境电商运营合成数据包"]];
readme.getRange("A1:F1").format = { fill: "#16324F", font: { name: "Aptos Display", size: 18, bold: true, color: "#FFFFFF" }, rowHeight: 42, verticalAlignment: "center" };
readme.getRange("A3:B10").values = [
  ["数据性质", "完全合成，用于本地作品集演示，不代表真实店铺经营结果"],
  ["目标市场", "亚马逊美国站（字段与规则为简化演示口径）"],
  ["商品数量", products.length], ["库存快照", inventory.length], ["客服事件", tickets.length],
  ["价格口径", "美元；成本为假设的单件落地成本"],
  ["销量口径", "近30天销量为模拟月销量；近7天日均销量用于库存预警"],
  ["标识说明", "品牌名、商品编码、模拟商品编码和评价内容均为虚构"],
];
readme.getRange("A3:A10").format = { fill: "#EAF0F6", font: { bold: true, color: "#16324F" } };
readme.getRange("A3:B10").format.wrapText = true;
readme.getRange("A3:B10").format.borders = { preset: "inside", style: "thin", color: "#DCE5EE" };
readme.getRange("A:A").format.columnWidth = 18;
readme.getRange("B:B").format.columnWidth = 66;

const productRows = [
  productSheetHeaders,
  ...products.map((row) => [...row, null, null, null]),
];
productSheet.getRange(`A1:P${productRows.length}`).values = productRows;
productSheet.getRange("N2").formulas = [["=E2*G2"]];
productSheet.getRange(`N2:N${productRows.length}`).fillDown();
productSheet.getRange("O2").formulas = [["=IF(E2=0,0,(E2-F2)/E2)"]];
productSheet.getRange(`O2:O${productRows.length}`).fillDown();
productSheet.getRange("P2").formulas = [["=IF(J2>=75,\"高竞争\",IF(K2/L2<=M2,\"断货风险\",IF(O2>=0.6,\"高毛利候选\",\"常规观察\")))"]];
productSheet.getRange(`P2:P${productRows.length}`).fillDown();
styleSheet(productSheet, productSheet.getRange(`A1:P${productRows.length}`), [["A", 19], ["B", 14], ["C", 54], ["D", 22], ["E", 11], ["F", 11], ["G", 14], ["H", 10], ["I", 14], ["J", 17], ["K", 12], ["L", 12], ["M", 14], ["N", 22], ["O", 18], ["P", 18]]);
productSheet.getRange(`E2:F${productRows.length}`).format.numberFormat = "$#,##0.00";
productSheet.getRange(`N2:N${productRows.length}`).format.numberFormat = "$#,##0";
productSheet.getRange(`O2:O${productRows.length}`).format.numberFormat = "0.0%";
productSheet.tables.add(`A1:P${productRows.length}`, true, "ProductsTable").style = "TableStyleMedium2";

const inventoryRows = [inventorySheetHeaders, ...inventory.map(row => [...row, null, null, null])];
inventorySheet.getRange(`A1:G${inventoryRows.length}`).values = inventoryRows;
inventorySheet.getRange("E2").formulas = [["=IF(C2=0,999,ROUND(B2/C2,1))"]];
inventorySheet.getRange(`E2:E${inventoryRows.length}`).fillDown();
inventorySheet.getRange("F2").formulas = [["=IF(E2<=D2,\"紧急\",IF(E2<=D2+7,\"预警\",\"健康\"))"]];
inventorySheet.getRange(`F2:F${inventoryRows.length}`).fillDown();
inventorySheet.getRange("G2").formulas = [["=MAX(0,ROUND((D2+14)*C2-B2,0))"]];
inventorySheet.getRange(`G2:G${inventoryRows.length}`).fillDown();
styleSheet(inventorySheet, inventorySheet.getRange(`A1:G${inventoryRows.length}`), [["A", 21], ["B", 13], ["C", 13], ["D", 16], ["E", 16], ["F", 16], ["G", 23]]);
inventorySheet.tables.add(`A1:G${inventoryRows.length}`, true, "InventoryTable").style = "TableStyleMedium2";
inventorySheet.getRange(`F2:F${inventoryRows.length}`).conditionalFormats.add("containsText", { text: "紧急", format: { fill: "#FDE7E9", font: { color: "#A52230", bold: true } } });
inventorySheet.getRange(`F2:F${inventoryRows.length}`).conditionalFormats.add("containsText", { text: "预警", format: { fill: "#FFF1D9", font: { color: "#95600D", bold: true } } });
inventorySheet.getRange(`F2:F${inventoryRows.length}`).conditionalFormats.add("containsText", { text: "健康", format: { fill: "#E0F5EA", font: { color: "#14704C", bold: true } } });

const ticketRows = [ticketSheetHeaders, ...tickets.map(row => [...row, null, null, null])];
ticketSheet.getRange(`A1:H${ticketRows.length}`).values = ticketRows;
ticketSheet.getRange("F2").formulas = [["=IF(AND(B2<=2,C2=TRUE),\"P\"&0,IF(OR(B2<=2,C2=TRUE),\"P\"&1,\"P\"&2))"]];
ticketSheet.getRange(`F2:F${ticketRows.length}`).fillDown();
ticketSheet.getRange("G2").formulas = [["=IF(F2=\"P\"&0,\"售后退款\",IF(F2=\"P\"&1,\"客户风险\",\"一般反馈\"))"]];
ticketSheet.getRange(`G2:G${ticketRows.length}`).fillDown();
ticketSheet.getRange("H2").formulas = [["=IF(F2=\"P\"&0,\"2小时联系 / 48小时闭环\",IF(F2=\"P\"&1,\"24小时响应 / 72小时闭环\",\"3个工作日内跟进\"))"]];
ticketSheet.getRange(`H2:H${ticketRows.length}`).fillDown();
styleSheet(ticketSheet, ticketSheet.getRange(`A1:H${ticketRows.length}`), [["A", 21], ["B", 10], ["C", 20], ["D", 34], ["E", 76], ["F", 11], ["G", 20], ["H", 27]]);
ticketSheet.getRange(`D2:E${ticketRows.length}`).format.wrapText = true;
ticketSheet.getRange(`D2:E${ticketRows.length}`).format.rowHeight = 43;
ticketSheet.tables.add(`A1:H${ticketRows.length}`, true, "TicketsTable").style = "TableStyleMedium2";
ticketSheet.getRange(`F2:F${ticketRows.length}`).conditionalFormats.add("containsText", { text: "P0", format: { fill: "#FDE7E9", font: { color: "#A52230", bold: true } } });
ticketSheet.getRange(`F2:F${ticketRows.length}`).conditionalFormats.add("containsText", { text: "P1", format: { fill: "#FFF1D9", font: { color: "#95600D", bold: true } } });

for (const [sheetName, range, filename] of [
  ["数据说明", "A1:B10", "readme.png"], ["商品主数据", "A1:P10", "products.png"],
  ["库存快照", "A1:G21", "inventory.png"], ["客服工单", "A1:H12", "tickets.png"],
]) {
  const preview = await workbook.render({ sheetName, range, scale: 1.25, format: "png" });
  await fs.writeFile(path.join(previewDir, filename), new Uint8Array(await preview.arrayBuffer()));
}

const inspection = await workbook.inspect({ kind: "table", range: "库存快照!A1:G21", include: "values,formulas", tableMaxRows: 21, tableMaxCols: 7, maxChars: 12000 });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "formula error scan" });
await fs.writeFile(path.join(outputDir, "verification.txt"), `${inspection.ndjson}\n${errors.ndjson}\n`, "utf8");

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, "亚马逊美国站运营模拟数据.xlsx"));
console.log(JSON.stringify({ products: products.length, inventory: inventory.length, tickets: tickets.length, outputDir }));
