# V7 spec freeze manifest (v7-ml-spec-frozen)

Every file in the freeze commit, with the SHA-256 of the committed blob.

**Line endings.** The repository stores LF line endings (`.gitattributes`: `* text=auto eol=lf`). Several Stage-0 files were written on Windows with CRLF line endings. SHA-256 values quoted inside the Stage-0 evidence documents (for example script and result hashes in `rt5-input-availability.md` and `m31/M3-1-settlement-feasibility.md`) were computed on those working-tree (CRLF) bytes before the commit.
- The last column gives that working-tree hash where it differs.
- Content is identical after CRLF → LF normalization.

This manifest does not contain its own hash.

| File | Bytes (blob) | SHA-256 (committed blob) | SHA-256 (pre-commit working tree, if different) |
|---|---|---|---|
| `docs/results/v7-stage0-audit.md` | 18859 | `1145c1fb8ac6652808d31177c4ab028e56114044b243499888f4753d8883de57` | `b8ddb87c882cc29e2c6be4f2eaffc7e288a38b8b9dcb43b550f799f64c650fa5` |
| `docs/results/v7-stage0/BLOCKERS_FOR_USER.md` | 27137 | `41a7525c6482e4ec6eb920a3f58ffe1692c5bd34780e55bd9e7eebd7a4890adf` | `23c0bd083d8efb115baad49ab849030296eee5c9e820d540ceeb3dcdcde9a3bb` |
| `docs/results/v7-stage0/INCIDENT-2026-10-14-seal.md` | 7550 | `beb3cb085397c6f5d8b2481bf899ce304b202b16a5679c9a5d1e32c85152380e` |  |
| `docs/results/v7-stage0/MORNING_REPORT.md` | 6242 | `458ba88cffd785cb0112f83716b19c35e7fc71de947d5ec82d8611621763f8a3` | `84197154ecb11b53c53316886eabeac7dd2b23b8899b13b96243ec37f0a065f3` |
| `docs/results/v7-stage0/bar_audit.json` | 1125 | `1a49229701e6f8cf09576d22c2a94f110b1365b904b0ffeac9465dc2c7fcadee` | `a87742000fbb575ad0f5adb02d69d112779b410e51cb1afee7781a6678f571c5` |
| `docs/results/v7-stage0/bar_audit.symbols.csv` | 44565 | `13371949f02bc169539eb5b3da39273345b7ba8e842733738222f3a1ff46a5b9` | `a9d8efa9f57283f9c288edb1b48d753b3e24796011de4c2ee9f1d19cc7f1f4b7` |
| `docs/results/v7-stage0/binance-fee-evidence.md` | 3117 | `63a9fa95ca32ea7d0015fd19dd7cce2d3549cec6b3feb740761794f5e135efb7` |  |
| `docs/results/v7-stage0/binance-fee-evidence.md.sha256` | 113 | `099e5cd5e2c2efd612bfbc3471ae1f4f1659ec4e001e29095647ad3383e61245` |  |
| `docs/results/v7-stage0/d0_eval.json` | 5835 | `d81e2a0edc148e1732a35535e7aa4b935869118a9a341e66990673cf0af68c2f` | `bb5e03f0edd7c90c06690aca888ca87a872026f18bb7a61d2cacd2c606f74a3c` |
| `docs/results/v7-stage0/delisting_coverage.csv` | 2191 | `d7467cffc86db6e81551d6fd5e4374ab7e8ddc0f163c254a518eededded3f92c` | `ce97406292fd0edaa7600ae0a111b01b9b85fa1f564608fccfa3cf1566efe458` |
| `docs/results/v7-stage0/ec/ec9_records.json` | 10529 | `f3a1a85c02874329146a1eae09cd1d184b89a15b39beb8831ab0690428e43cc7` | `3070a8efca8009c759c2868bcbda503435d47b613315603a3bb0788000a115c4` |
| `docs/results/v7-stage0/f7_f15_coverage.json` | 352 | `109bf49f16b5059935fb248a0a0ce7cdce16ebe7371fd9992fcf668574c77295` | `3685a6964fb6c70c5d26ec11947c1ec143f1c7475054c1be034eaf6ca542f0b7` |
| `docs/results/v7-stage0/funding_audit.json` | 255552 | `f0fbc6dfce57a87d5be8e10688abd722ecf8ba395aad5d5ee43eda576730dcf7` | `e7e9c9cbe86e0e42bf616d53430778906814c042df47b6bf634c2c9c83285b6c` |
| `docs/results/v7-stage0/listing_summary.json` | 8418 | `4da67608cd16adc26da375c639da5c77dead5a85827fbc57d0b0bc56f82dbcf6` | `721decf70d6a846137060a27ec53d46fc36f231d73068f81f0dce3637d3d06f9` |
| `docs/results/v7-stage0/m31/M3-1-settlement-feasibility.md` | 11773 | `fceb7ddf590ddc61fd051122f48285f915a35adcbe4f4ede4c3332e056d791a0` |  |
| `docs/results/v7-stage0/m31/archive_dataset_and_index_intervals.txt` | 2373 | `7b5ff7a1913980271aa85be69340f088fad75c1fd604262a68d75c277e85ba53` |  |
| `docs/results/v7-stage0/m31/classification.json` | 23548 | `aea4f11e06bc951ad7756f486d92b334c4bbce50b3831d1d2343c3a7c82620f0` | `fcfe4e8146cc4ea462482826aaa28100e0b62b80e691c774be0c47d050e2cff5` |
| `docs/results/v7-stage0/m31/events.json` | 9300 | `530c99017e88e1ebaf0006f69439f26a2d5b0bf063d2fc05f84a9076d967ba1e` | `8070b202ee49fce9a5656d1cc5992103727ab57b5755f081bfa6e414d4f1ebe6` |
| `docs/results/v7-stage0/m31/notices.json` | 302920 | `db3fce422a2cf74ae5bb364c924bd10506ccd076a90b5c9e5c04cffa3063eb57` | `c10d875212066900a1e8ad285ea4b09b683c0f7229d479c773a436f487636fc2` |
| `docs/results/v7-stage0/m31/notices_errors.json` | 2 | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` |  |
| `docs/results/v7-stage0/m31/review.txt` | 38685 | `2d4b607aafe405bddd00080fa926ac2901cf06fdbb2007f3efeed4ae53441efe` | `10deaadf6d82051cf647d719db742f217bf7a59cd2077345b21fb7c3b8616092` |
| `docs/results/v7-stage0/m31/table.md` | 5010 | `b97bbe09a61d7a0831bcd94a40a9a152cc8a33a0f38f71ddf67ff2def9fde015` |  |
| `docs/results/v7-stage0/r6_coverage.json` | 183 | `f6a97bef2f7cf3ebeb10756fcd0f615b3b01d4b3fa1cbc5dea76bc117d0d00af` | `e55f43663002741b218d2afc84c08a4b942d302f557732e0607b297cf8193069` |
| `docs/results/v7-stage0/rt5-input-availability.md` | 11319 | `53637fede594ce31e27d6166ba25c17a07626f3dd7b42ee9140b98e3eea027dd` |  |
| `docs/results/v7-stage0/rt5-summary.json` | 2474 | `065ba8987c9c0abf4bed97a017b294552b79b374833de0e64bc986d92e73d4f4` | `6ef12ee5ff5261ca0c69eb0a0d8dc7c20590157e8aa7f22fcc1711ac858b0c8a` |
| `docs/results/v7-stage0/scripts/bar_audit.py` | 3125 | `57027b70d08db8150deb78405a476082ff038f9bbd3ab6112e8a18eabc24f787` |  |
| `docs/results/v7-stage0/scripts/coverage_scripts.py` | 3498 | `97c7b5c26dfa4e5f86600a5f647f031f1c602b0047bc72bceb6c1cff5b95e3e7` | `596ee669af377627a906d873751fc35d317467ed6e7149b73053f62e71f8a89c` |
| `docs/results/v7-stage0/scripts/d0_eval.py` | 6663 | `25cab6e758f4b72c759448599bbbc1b5a9b187f889d4777a4baee5b416e54d71` | `fa71f4b5a5ceb1506b3aa41e0b55eb683ab180b65324e1c86b727f79e600527c` |
| `docs/results/v7-stage0/scripts/ec1_check.py` | 1622 | `e9681f5567ff9a0d205ae06abfcc517b26d38cf6aabea813205f89a69d78c83f` |  |
| `docs/results/v7-stage0/scripts/ec9_check.py` | 2022 | `4d3dd0ce4ce72a4590bb8e7c10dbf229390fddfe46f7f2607b24e4945e360137` |  |
| `docs/results/v7-stage0/scripts/fetch_bodies.py` | 742 | `dc6257461fd46e7944a8f6f48e3e206bf88c39182a36289bed00842a93678605` |  |
| `docs/results/v7-stage0/scripts/fetch_funding_archive.py` | 2327 | `89033f63beddb8048211223aab7113e04f71acaca11c1eb2d1cd90e213866817` |  |
| `docs/results/v7-stage0/scripts/fetch_funding_rest.py` | 2651 | `606af7965e542e9b26fb67b548ec8a217167580d20989b2a98358ef5cb7b24a8` |  |
| `docs/results/v7-stage0/scripts/fetch_klines.py` | 4760 | `0130c9dbf28d618d50c44986c21e19f74ab2c0b2f9a987e3ad336143f257c79d` |  |
| `docs/results/v7-stage0/scripts/funding_audit.py` | 5183 | `97624be89b46d58903b335ca13ba1dd402c99127b6eba5098ea4f0c6bf0d08c7` | `dedb02f0589a7a79f76d46fc751ea72d999a2fad783823f1c3b2a9043d94a607` |
| `docs/results/v7-stage0/scripts/list_archive.py` | 4142 | `2f525d634183326bd47bdc3746bc82e3807adac357e1d46cd2e4154f03c30b16` |  |
| `docs/results/v7-stage0/scripts/listing_summary.py` | 3672 | `83b27b835ca8f2a59e2ef0371aedb9937205817183561ba29618e9c1102ef34a` |  |
| `docs/results/v7-stage0/scripts/m31_classify.py` | 7834 | `4e62f34c019afff3fed60a9a8bc22d75057c4f3a0ac7a88585b2bb09c8fe738b` | `6cdcc18686ab96dff4f5e52b8e618c4dfe8c6e988b78441f3f33265afd22a05c` |
| `docs/results/v7-stage0/scripts/m31_events.py` | 2450 | `9186d6ee6bd57b81ca6064161ae67b0a03e1c9c719d8696a1f737b2d0df86538` |  |
| `docs/results/v7-stage0/scripts/m31_notices.py` | 9081 | `05ac939fea5c999d947573c7cba476d3d386838f8194ca1785e2b12dd5329ff5` |  |
| `docs/results/v7-stage0/scripts/rt5_availability.py` | 16764 | `2c8b22960f9cd4e73f337ca44b78b5e94ccb4ed1d27b30daafe50a4d667ed623` | `946ff21e0cf96ea7f5c52b52ea0a93e00f805799cecb1ea8cccedf9d51a65b7f` |
| `docs/results/v7-stage0/scripts/rt5_v2_dec35.py` | 15754 | `7b28812728eea4fc95fadece13d8bd313b2268012db55ed8faab22a79abd10fa` | `d192b703a085b67fc85f2ed4718ea121a084d6c38b5e970c3c2454ee045a59c3` |
| `docs/results/v7-stage0/scripts/rt5_v3_r3.py` | 17395 | `3ada420f379c2ab8e8d177ef55508263b2a659c8e46cc28ddd4f452e460f8e4c` | `4b1b6f7b5fedbd573414d0b91e4c9aacd3e55e2780b6a9521c2e59758b100ed5` |
| `docs/results/v7-stage0/scripts/rt5_v4_ec.py` | 1964 | `9ff4e0fdd8b9e9994bcbdc35f634b5b3516cacedf3856a9d43c87282e4922b19` |  |
| `docs/results/v7-stage0/scripts/wayback_notice_lag.py` | 2100 | `b6650ca40868b427ac05e413d5d5717e36d922bea722d9c618874721187824a0` | `c9f4e67407dc70bbbc0acbbbcecc2c7ae726c782be23c7df0f64179446089565` |
| `docs/results/v7-stage0/scripts/wayback_prepub_check.py` | 2558 | `f13c539052e30c6bc9bbee88f8633e77d89d39b6fbda83da7e24381dbaa651d1` |  |
| `docs/results/v7-stage0/stage0-data-manifest.json` | 368034 | `9f08a59a2cb4e74449bb644fbfa938a436bf2206b7f20cb99e8f860c92e70556` | `70b5f80dbbb4ff8069cdeecd436a042a2865efee9a5f851daf2e6208ab733569` |
| `docs/results/v7-stage0/symbol_trading_history.csv` | 22990 | `b340f4bd32166d8dcc59c18f0c8857815db332d91146663c9230441c5206c346` | `8578d3c09cc01fd7e82a46c0985ec1a715de9fdde5d859abdef2b1eabadf3f18` |
| `docs/results/v7-stage0/v7-prefreeze-redteam-r2.md` | 27999 | `3993b72c79e88bf2d7856a7b8ccfe30b5b1f7d6349cc4afe257cb4e46dd0ae99` |  |
| `docs/results/v7-stage0/v7-prefreeze-redteam-r3.md` | 25632 | `62d49bccf3d78ae439281be12bc687e8b7bbb3474736abb1448e6e862fc27d66` |  |
| `docs/results/v7-stage0/v7-prefreeze-redteam-r4.md` | 20252 | `5c764e04ddcd15db7f8116b17b9cbba1d53dc53b52f44bce143f55e4a3e737ec` |  |
| `docs/results/v7-stage0/v7-prefreeze-redteam.md` | 22356 | `ec0df7789ab0c5571a507d4b5b39083b57bbeec5c86934b260128dc2e5031867` |  |
| `docs/results/v7-stage0/v7-resource-plan.md` | 5480 | `2fdc7f79e2c2c7049533591f103bd0ee3c4ba255748ae38ec4b2672fc1eef94b` |  |
| `docs/results/v7-stage0/wayback_notice_lag.json` | 23218 | `7248a830a970a730539e118ba3f4b50cc407a6bed3a5e71e6b1da2414f680158` | `e590b23200f9ee68deaf4bd9918a260b41d9faf81138026000be2e7242e5061b` |
| `docs/results/v7-stage0/wayback_prepub_check.json` | 4677 | `830ecffe790951f8daaeb522f97a1fa52fa354ef2eafed5d42a13d0303594658` | `7285efcd96680b9ae275ab691bbad772583952424bfb526a04a44d50e279d63d` |
| `docs/superpowers/specs/2026-10-14-luna-v7-draft1-review-patch-rev2.md` | 23303 | `74688af319f1b73db7963ff86d0fd5818ca4e249466935b4546562c9deb35403` |  |
| `docs/superpowers/specs/2026-10-14-luna-v7-ml-research-design.md` | 107676 | `ce7370fa5cd49a47ae0912a46430f2612c972108536d85f2d8306e0389db9dd4` | `dfee0cbd6dd169cdb40a60e01c7f1410bb09c38a9f9868b45721302609d9aec0` |
| `docs/superpowers/specs/2026-10-14-luna-v7-review-patch-rev2_1.md` | 8247 | `13a070e942c8ce255c2133a65fa47ad67a32f8a5aa3a6e33d60370e1f3836bf0` |  |

**Excluded on purpose:**
- `docs/results/v7-stage0/OVERNIGHT_PROGRESS.md`: a temporary progress log that no record references.
- `var/` (gitignored): raw market data, caches, the failed / zero-byte fee downloads, the UK spot-fee PDF, Wayback fee captures, and the partial RT-5 v4 outputs.
- Changes in the main `LunaTradingBot` worktree: unrelated to V7.
