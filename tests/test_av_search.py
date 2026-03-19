import unittest
from types import SimpleNamespace

from bot.services.av_search import AVSearchService, is_av_code_query, normalize_fc2_code


def _make_service() -> AVSearchService:
    settings = SimpleNamespace(
        av_http_timeout_sec=15.0,
        av_max_results=18,
        av_enabled=True,
        av_javbus_base_url="https://www.javbus.com",
        av_madouqu_base_url="https://madouqu.com",
        av_dmm_base_url="https://www.dmm.co.jp",
        av_fc2_base_url="https://adult.contents.fc2.com",
    )
    return AVSearchService(settings)


class AVSearchServiceTests(unittest.TestCase):
    def test_fc2_code_normalization_and_query_detection(self) -> None:
        self.assertEqual(normalize_fc2_code("fc2-ppv-4863846"), "FC2-PPV-4863846")
        self.assertEqual(normalize_fc2_code("FC2PPV4863846"), "FC2-PPV-4863846")
        self.assertTrue(is_av_code_query("FC2-PPV-4863846"))
        self.assertFalse(is_av_code_query("4863846"))

    def test_parse_dmm_search_extracts_code_and_date(self) -> None:
        svc = _make_service()
        html = """
        <div class="flex py-1.5 pl-3">
          <a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/?i3_ref=search&i3_ord=1">
            <div class="flex-shrink-0 flex justify-center items-center h-[180px] w-[130px] mr-3">
              <img src="https://pics.dmm.co.jp/mono/movie/adult/wanz530/wanz530ps.jpg">
            </div>
          </a>
          <div class="flex-auto flex flex-col">
            <a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/?i3_ref=search&i3_ord=1">
              <div class="mr-3">
                <div class="space-x-1"><span>DVD</span><span>通販</span></div>
                <p class="text-sm font-bold line-clamp-2">推川ゆうりの凄テクを我慢できれば生★中出しSEX！</p>
              </div>
            </a>
            <a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/?i3_ref=search&i3_ord=1">
              <div class="flex-grow mr-3">
                <p class="text-xs text-gray-500">発売日：2016/09/01</p>
                <p class="text-xs text-gray-500 line-clamp-1">出演者：推川ゆうり</p>
              </div>
            </a>
          </div>
        </div>
        """

        items = svc._parse_dmm_search(html, base_url="https://www.dmm.co.jp/search/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "dmm")
        self.assertEqual(items[0].code, "WANZ-530")
        self.assertEqual(items[0].url, "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/")
        self.assertEqual(items[0].date, "2016/09/01")
        self.assertIn("推川ゆうり", items[0].summary)

    def test_parse_dmm_detail_extracts_fields(self) -> None:
        svc = _make_service()
        html = """
        <html>
          <head>
            <title>推川ゆうりの凄テクを我慢できれば生★中出しSEX！ - アダルトDVD・ブルーレイ通販 - FANZA通販</title>
            <meta property="og:image" content="https://pics.dmm.co.jp/mono/movie/adult/wanz530/wanz530pl.jpg">
            <meta name="description" content="第28弾は、ホルスタイン系むっちむちGカップ。">
          </head>
          <body>
            <h1>推川ゆうりの凄テクを我慢できれば生★中出しSEX！</h1>
            <div>
              発売日： 2016/09/01
              収録時間： 180分
              出演者： 推川ゆうり
              監督： ----
              シリーズ： 我慢できれば生中出しSEX！
              メーカー： ワンズファクトリー
              レーベル： WANZ
              ジャンル： 巨乳 素人 単体作品 中出し 手コキ
              品番： wanz530
              平均評価： 3.05
            </div>
          </body>
        </html>
        """

        detail = svc._parse_dmm_detail(html, "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.code, "WANZ-530")
        self.assertEqual(detail.date, "2016/09/01")
        self.assertEqual(detail.runtime, "180分")
        self.assertEqual(detail.studio, "ワンズファクトリー")
        self.assertEqual(detail.publisher, "WANZ")
        self.assertEqual(detail.series, "我慢できれば生中出しSEX！")
        self.assertEqual(detail.actors, ["推川ゆうり"])
        self.assertIn("巨乳", detail.genres)
        self.assertEqual(detail.score, "3.05")

    def test_parse_fc2_search_extracts_article_code(self) -> None:
        svc = _make_service()
        html = """
        <div>“人妻” 搜索结果 12件</div>
        <div class="c-cntCard-110-f">
          <div class="c-cntCard-110-f_thumb">
            <a href="/article/4866204/" title="【素人個撮♥】清楚で品の良いセレブ人妻にナンパ中出し！！［８］" class="c-cntCard-110-f_thumb_link">
              <img src="//contents-thumbnail2.fc2.com/w360/storage200000.contents.fc2.com/file/404/40345189/1773846517.48.png">
              <span class="c-cntCard-110-f_thumb_num">19:27</span>
            </a>
          </div>
          <div class="c-cntCard-110-f_indetail">
            <span>by </span><a href="/users/kosatsuya/">個撮屋本舗</a>
            <a class="c-cntCard-110-f_itemName" title="【素人個撮♥】清楚で品の良いセレブ人妻にナンパ中出し！！［８］" href="/article/4866204/">【素人個撮♥】清楚で品の良いセレブ人妻にナンパ中出し！！［８］</a>
            <span>2980 pt</span>
          </div>
        </div>
        """

        items = svc._parse_fc2_search(html, base_url="https://adult.contents.fc2.com/search/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "fc2")
        self.assertEqual(items[0].code, "FC2-PPV-4866204")
        self.assertEqual(items[0].url, "https://adult.contents.fc2.com/article/4866204/")
        self.assertIn("19:27", items[0].summary)
        self.assertIn("個撮屋本舗", items[0].summary)

    def test_parse_fc2_search_ignores_recommendations_on_zero_results(self) -> None:
        svc = _make_service()
        html = """
        <div>“FC2-PPV-4863846” 搜索结果 0件</div>
        <div class="c-cntCard-110-f">
          <a class="c-cntCard-110-f_itemName" title="推荐商品" href="/article/1111111/">推荐商品</a>
        </div>
        """

        self.assertEqual(svc._parse_fc2_search(html, base_url="https://adult.contents.fc2.com/search/"), [])

    def test_parse_fc2_detail_extracts_code_runtime_and_tags(self) -> None:
        svc = _make_service()
        html = """
        <html>
          <head>
            <title>FC2-PPV-4863846 高額ギャラに目がくらんだ駅伝代表選手１８歳　例のごとく複数人で囲み全員の体力が尽きるまで終わらない連続生挿入の一部始終 | FC2电子市场</title>
            <meta property="og:image" content="https://contents-thumbnail2.fc2.com/w276/storage200000.contents.fc2.com/file/377/37690728/1773488024.5.png">
            <meta name="description" content="FC2 详情说明。">
          </head>
          <body>
            <div>
              46:57
              by 模倣犯
              商品标签 ハメ撮り 素人 個人撮影
              支持的设备 PC iOS Android
              上架时间 : 2026/03/14
              商品ID : FC2 PPV 4863846
              平均评价 5
            </div>
          </body>
        </html>
        """

        detail = svc._parse_fc2_detail(html, "https://adult.contents.fc2.com/article/4863846/?dref=search_id")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.code, "FC2-PPV-4863846")
        self.assertEqual(detail.title, "高額ギャラに目がくらんだ駅伝代表選手１８歳 例のごとく複数人で囲み全員の体力が尽きるまで終わらない連続生挿入の一部始終")
        self.assertEqual(detail.runtime, "46:57")
        self.assertEqual(detail.date, "2026/03/14")
        self.assertEqual(detail.studio, "模倣犯")
        self.assertIn("素人", detail.genres)
        self.assertEqual(detail.score, "5")


if __name__ == "__main__":
    unittest.main()
