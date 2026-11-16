"""Independent exact holds for 36 timeout order-success receipts.

Original UNKNOWN is never converted to delivery success. No sender or writer.
The prior 13 reviews, 73 freezes, six holds and cancellation hold stay separate.
"""
import hashlib,json,os,fcntl,re,logging
from pathlib import Path

APPROVED_ORDER_IDS=(17110, 17111, 17112, 17113, 17114, 17115, 17116, 17117, 17118, 17119, 17368, 17656, 17657, 17658, 17659, 17660, 17661, 17662, 17663, 17664, 17665, 17666, 17667, 17721, 17722, 17723, 17724, 17725, 17726, 17727, 17728, 17741, 17742, 17743, 17744, 17745)
SCOPE=({'order_id': 17110,
  'source_identity_sha256': '1df25b87d7c1dca2664936799309878dc94e7a45c80c5cafd9d9e4fdbb6fb28a',
  'source_pair_sha256': '20da066bab07687d02cd9bcaab601e8248f6ef8a708150e3cf816d6fef72fbd7',
  'number_sha256': '19e9048624287d948cc4e460d9c1813e5bddece14034026949397030cd0246a9',
  'public_number_sha256': '09b02d9b83469ce0737a24029e2d0b3374131a6008dcb68a4ccf4fc78baa91d4',
  'attempt_ref': '0ea1c1d68852050a4b6f7905cd7cf477721a37dd9d60af6ca92ff6a72d2774bd',
  'original_sending_sha256': 'b279a8ab0909f68b03f1ffb9d414748b06b27539a5f99a1c0875a1fbe991d317',
  'unknown_row_sha256': '98f2d59ea1cf2b9f7ecd5d9e0ba302d3a7e3ee531ca42d3499d96f254444c0c1',
  'terminal_links_sha256': '99d16e5708cdef8a2df4184afada5ed14d7ba8f2a44f722d8f1b9f501122f81c',
  'job_metadata_sha256': '60e67138ce27b8db5d04666a50da10933e0137ac7036861c0f9fd3285ea9a1d6',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17111,
  'source_identity_sha256': '0689761cf76c0376f8d91e61c42f49bb3ca89d06fbe6880b4d542c793fac45df',
  'source_pair_sha256': '78d90e5f8b9c82d377281bec1ca9d6fdb144b9638796ea0cd5a8f05148a2efed',
  'number_sha256': '150d6f9bcd7b2fa86def9a7dfae11e21fe25ba425143cb280f685791ae0848d5',
  'public_number_sha256': 'eb0102d3153206278fe7dd23669c9cfd91df743a284851234df006b7596f1870',
  'attempt_ref': 'a20ebb301ae37129139f929dff0ca6ef97fd0257b46c31cf99851e0f8ac998da',
  'original_sending_sha256': 'fcb48ce324a64b81fbdec49a84593f6ebcab07f747ab6a853b4abda2c40763ed',
  'unknown_row_sha256': '92bdaa6ee7072add7202b00814ab4b36699ba50784cdf8581d18f24178c4d04f',
  'terminal_links_sha256': '19b5b3847d46b4b6e856795132d1a2ab05c383837d43b781a5593a6cb8b104f3',
  'job_metadata_sha256': '0e9097ee1c4d916cad596bca3265f983d8277e7f0c1980f811c428a64e69e998',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17112,
  'source_identity_sha256': '7fc512a445c5cce811a14b894b56678bf784f9c346112645fc8dfd869ca35372',
  'source_pair_sha256': '60a9e2ad22b67c4c0527c7c25fd242f8b1c54b0d7e8fac712855a635e765fd07',
  'number_sha256': 'ad85cc2581ab5a5a3af425bcdb514bb76e427d9811c248cdf934596bb09febd3',
  'public_number_sha256': 'e5fef273d86af303e0f2d76693e60cc8f952bc60417b8094118b46210f7163cc',
  'attempt_ref': '8cc1b007a1a42c1773bc83776314ff76806024c1a68b56df20e8478d19aa4301',
  'original_sending_sha256': 'db7abf5eb83a9a3af2e4a82e1a074a8cc120a6b94057a7daccf63e7183de551f',
  'unknown_row_sha256': '270513c173922900439408afb12e59d4fb6c2fd102e1e288de41e260bcd7273b',
  'terminal_links_sha256': '4afdbd8ced909242264445c1762a7ca80f3380cdf6b2eada929d4f56b6db4a2c',
  'job_metadata_sha256': 'a29b6539c2178108a5366cab92d3f3058a9102e2ee2da9f2118b1cbf596c688f',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17113,
  'source_identity_sha256': '0fcfc7eac7b82464bfb6e307e98bf32c9480fbc97f894e3e49619ae5213c3ee3',
  'source_pair_sha256': 'ba7a7f6ac5fde1120ce4d3b462786812ba9fe8d8fbe16c5074edcad8b3ad38ce',
  'number_sha256': 'c6c58610364ec4147e69eb9d24be3a79e8e5d4634a06d60ff9415c79332b9a2e',
  'public_number_sha256': '5fc214ef70a978fd495b11f4190c4f7b4bd45f9d842cdbcfc9244d47ffb817a6',
  'attempt_ref': '61b9c6561202d570ce7e44d62a8e9d0b662ca075d34ab9b4378df2b210f054cb',
  'original_sending_sha256': '4781b2fcedc3079258907930a8997647050364f39ed45d05a9c64a78465e7e47',
  'unknown_row_sha256': '7995bf1e0682dfaefeb7c91676275583d6c290db71f5b34bcc0ffdc0be59224d',
  'terminal_links_sha256': 'd00a239871cf99761b8ed72313ff58121506b0531b7728f78144ad2190f85afd',
  'job_metadata_sha256': 'd3c4e4d5d94595218cd96462777abb149cd8174c8e5187bb0f2f304847274a9d',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17114,
  'source_identity_sha256': 'dd91cc8bfc447d222788f3fc14f5eb31b2ed17794ed4a19953de444b2f7890b6',
  'source_pair_sha256': '7e2e345323bbffb97c42564870d8bb442554f95b4190f1720bc874f987a545e7',
  'number_sha256': 'e76de506ae8a3b4556115d52362937f1aa51c61228345a822e828fa0c42e2df1',
  'public_number_sha256': '840b9369270b378a81ff3a1e8109f235951584ae5bbdcf26e878c800254c3d4b',
  'attempt_ref': '832caada1726e3b712bf842f5f9194c14da9c88371439baa56573f0ba0a25f72',
  'original_sending_sha256': 'baeeacc62cadc6a859d6b2ffe627702e4c9593261b6906b49381214d8de0a1c9',
  'unknown_row_sha256': '12f5ee02c0cacf6adc1e84ce1854a6b8ca975c587ae883f51059e3fba1b096ba',
  'terminal_links_sha256': 'b27faece2d60da7becbccaf1c85f45e6fcc1b7bce94ce9a14c3e8174cfe8e0a6',
  'job_metadata_sha256': '02bde81d798d64085697a138272e649f82f837a61025a1b51dd11c2f511dfc16',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17115,
  'source_identity_sha256': '1ad8f8151616e866114b926768a2f075000ece1c84266a3efdd91fc1be30adea',
  'source_pair_sha256': 'e76ee77ee48a724f10d222f607bac05449d7916808aec3d99f5273dc175436d1',
  'number_sha256': 'aafe68193b0dd58cd16b3439514b9ca4d1e5c6c7db5b60b1c163f5906f6f5bad',
  'public_number_sha256': '463e15d76a68a8112ef40dabb94ac02ad0ed6efa304f12f785e94756dc3a2609',
  'attempt_ref': '841602957d56b6345bec56a27368d64de1bd4527e2f075f779d99c48cd9dc121',
  'original_sending_sha256': '2e6d2b6d939e47ed8bcbb4b140d051ab7f96bb4c1d14b87db1417f7c47b8d416',
  'unknown_row_sha256': '7324ecb21654a81adff97f97b67a5506d5eb43cb69712be1fc909234b7474efa',
  'terminal_links_sha256': '63c549bee1182879f1a9d2eac2d731e36313d1c9cf279388189e74c104006473',
  'job_metadata_sha256': '7034d4906510d7125345a0e4920fa770377e35d0ac3e8ef28abf910d29591636',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17116,
  'source_identity_sha256': '3430a7d07d4b7bf4bd098f849c79e2cb88628e02dffbb8b1b904f0131eef2f47',
  'source_pair_sha256': '4c0c11fee122ee3ad97937383fcbd546c6ee0ed79550e2273cab3692a5523fff',
  'number_sha256': 'fd9e3eea27f747207c69feeb7d1441a374f9bd287df8c50a205da979e8011f7b',
  'public_number_sha256': 'dd24728893905582900c7b26741a95f9ccad239f2787d3f2e047759f884d956e',
  'attempt_ref': '20ea15b0541cef4765b09e346d74327bf6c61319a95507088f3d2ee184c24323',
  'original_sending_sha256': '8d07347af03a6bbb3fcdf0e17c15057388f074693d27a469b7ef5ca77a0064f7',
  'unknown_row_sha256': 'bd3ded40a41915c3e5b62f085f96465363ae8b902aefbd2abfb8eac0359dc84a',
  'terminal_links_sha256': '2c70fbaf512b4d9bc96e39d7623a2c8c0b962f1a1a8c34760e8ed43e84650283',
  'job_metadata_sha256': '66d44be21a71fb833be7b4dc855116ccddb06947c8e188bcd104785443bc8891',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17117,
  'source_identity_sha256': '9b99e9d0540173287ba70fa45f29050191e8c7b5245d551492ad4b2e173518dd',
  'source_pair_sha256': '5fe2a2a9ec0731068477e4e900110603c8de0fde983450d9fdf8c0fa6d239e7e',
  'number_sha256': 'fd6497bf77e8c6659ad21e61a43d8f2e73bf6baf2b2f5c584b26c86684a7b184',
  'public_number_sha256': '48ada749741015b56626383b48c0b476bba9423f84e39b9e2221262c17211237',
  'attempt_ref': '4c33733d92fbd64018f95840944af056cfe273d3a080e718c9d9accb5bb8a554',
  'original_sending_sha256': '80c4627574913eeccfe4b2a9f95f744f7a527bd644d1d4c113593ac908120465',
  'unknown_row_sha256': '09f39dffe2516c143a1d9eca17cea4ebdc0f8f0d9d71d3c38d0d951fc9cd3c10',
  'terminal_links_sha256': '15ba67dcb5d49a1b9357a59f3111715f7f54fee0c3ebc1a419571ce8b6533e12',
  'job_metadata_sha256': 'dab810d8ce9fc9ca08e8c4feb51badf5bc044266843fd20bfeeebb8458b7f3a6',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17118,
  'source_identity_sha256': '2fddae4c68b0c6d45c6274b7704a6c60812171460e56a62aad170071631e3eec',
  'source_pair_sha256': '06313cd684bb1bbc3b23e35791bd8a4ad4b76f6836ba6ee492d327e88c48e385',
  'number_sha256': 'c04486ae8155792921441ad064553edf446fe0415f3d704c160b0c6236e0b7f3',
  'public_number_sha256': '0785025386de496c0b7ca3cc6f7a2b961b5f42c596f393a8b7210e4b4880db12',
  'attempt_ref': 'fc2373ba728cb4c84bbae95909b1f3699bd45517575a81f286df513e7c9a4396',
  'original_sending_sha256': 'dcb1e6c3655bb1bf98fbaa83205ae51f803aa40f203e9c6eb9b068ebec310e4f',
  'unknown_row_sha256': '28a8da9de454275906bde5106fbae9ea17afad78e9c1c3796b0cf3bdb4778389',
  'terminal_links_sha256': '6e08a2d7e8e3b94453eb64aa330bb9ac4c9e381f7f82bfc6c55a0abecaff7da0',
  'job_metadata_sha256': '4c23a773a6893e85aed9ec83b83477d1378517390c0db1bf75fd30c04dd0cb80',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17119,
  'source_identity_sha256': 'ba72d8a72a848ba3fe9f325a76a5e876213d04d024e5000e0a2a0825d7f36557',
  'source_pair_sha256': '2cb21bfe9ee2e41e7bbb30e54046c847d2e3c6e187f351c682fedb0a4166ae1e',
  'number_sha256': '2bf883f576b0171d4a958bb242bf7dea4e516d3a0c2c5f3c3f9c5592c997c54a',
  'public_number_sha256': 'e89b0db5402b9e15d3e4c508762f2f26b2e8822b8a6770d7720af8814164e5f4',
  'attempt_ref': '95e222e73c36ffa7bcd72cfedf2bc77e86826b89699f897d2787257e78a19c4f',
  'original_sending_sha256': '670400c8bb5a529be11cd377ae615a2b0ca0221a0a5ccd618f7805a45a3131a6',
  'unknown_row_sha256': 'b82de342e3aee44cf6c2b0d5a54a247864a44ca17db7bba645d739be448b3abf',
  'terminal_links_sha256': 'b8c4ca82c52dba00d656809ff5fad3af2accb78c97b868eca86491407d8c5b5f',
  'job_metadata_sha256': '17ade52de33bcfe8a9e4da747fffb287b4f9054a9e7b81b0325746b419f3021b',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17368,
  'source_identity_sha256': 'f59b619fee933832cde30ef2b7052c9600cbd723778003c071af5f5f73d81119',
  'source_pair_sha256': '9fdf083b0003595ce3331615b1880b65d08d1c52764a3ba77bb6be157b0ec385',
  'number_sha256': 'ec63514b44824a031a85efa4b1a7dbd9456f923852dd9aae8ea25a2ecdca5146',
  'public_number_sha256': '1dabe9ce5757340bf82a0e3a5f5fa917e8546e1127ddf1623b15ee5154c21e63',
  'attempt_ref': 'd2bfbe01644601ad716334ab8b6c2f939f4ab960f80cbf37aa444524d569f6cb',
  'original_sending_sha256': '7d462a0140acd80218fe81a0b73b74fe3bbdf1ba31473badac78994ebe161247',
  'unknown_row_sha256': 'd1f8fad9a94cf1b98575db103ae0e248df037249eae2643e84e06d6c289cde4a',
  'terminal_links_sha256': '46a75f8d7ac7cba689ca56028388f6345ba26fce950ae6744777dfdf566cea1d',
  'job_metadata_sha256': 'd37a2999e8e4fe92098e6696666d7caa7e9d8bfd7093878f2818a5107b2a84b0',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17656,
  'source_identity_sha256': '073ec3203e1e8eae178efe3fafb2233f9ef93912c10d826e41ba1946eb0e3930',
  'source_pair_sha256': '2410dd65d5c0db9b9851e8e92e83bd263a063847d583970ba9199721fea30779',
  'number_sha256': 'a978bd9a6b11ff3197f695c12a199cbe208280bd7004f9ba27b06ff301ed9c76',
  'public_number_sha256': '037f0c6d0672c35d7cad9d25dbbae6f40ebebcbe0e2ece1fe5372862bbc7a98f',
  'attempt_ref': 'c02afc6ee658ea6e3dddeb77a54c833d02beee7cdaf3c342dff6b1214766e3ef',
  'original_sending_sha256': '8ba2a5761e8917a66d015b7f89687208380409337c2f7e465628775c87fa1aeb',
  'unknown_row_sha256': '68457ec4f29b6795b7740e7e418b216d4bd20c37492be4ab8bf19bad75d894a2',
  'terminal_links_sha256': '3bdf729c6490fb5fb9755d50c0cdd861992e67a8dae1f6e22b5d1d782b6b41f9',
  'job_metadata_sha256': '342c16bbd02ba6beee02ca338713c8c25017bfbc4f28120b54d9c736bae9c6d5',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17657,
  'source_identity_sha256': '02b3b5c38302f26e7e4e8759c50aaf35feeb48ffeb757c0f1fdad0f1e3b85427',
  'source_pair_sha256': '4fd91b3587fd4f5bf5016ee86f77b9f679ff1dce50126902ac3adaf310e763f5',
  'number_sha256': '62cf53224a6845c6bd30ce84dd8a2ee428f257acb2f4bfd653b0a3e621714c57',
  'public_number_sha256': '31d28ca4b598db0de23eca4867d66f65e04c68f184d1bac5105079995b77832d',
  'attempt_ref': '6591da58adca1aead12510f709499105a346445f73bdeb6689499536743d47a8',
  'original_sending_sha256': '8dcef4acd7b524e70ab0fff2a0e0f0e8e431c74d0a45d5fbe71c945630660a3d',
  'unknown_row_sha256': 'b23b16fe75b49995ea9671e66c8ccdbbe86d2312711f2d34d0d0dc133543de96',
  'terminal_links_sha256': '92e899a05bfc33176ddfa009ca07b69812e5f9abd8c459a83561cbc0a389c87a',
  'job_metadata_sha256': '9e1f9544a07dd5c536def0174386ca4adf172f8f0012185817334cde2dc883a3',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17658,
  'source_identity_sha256': 'c9cf7be8e8726e6132dd81cfd52c8435ac7561bf03e61c35f8cd3c688cfb314c',
  'source_pair_sha256': '7bd7d68f9294a31424cc53a88fb7e45df9c21b874eb192fb7560f98025a107b3',
  'number_sha256': 'dcab45dd3991a6b364a34a4247f08b6a8143981d2ba9da1cdcfae1b5e51b56da',
  'public_number_sha256': 'fd70e62df20102676746f10d22191a6bd5ff29bdb6b1bd5cb4fa7e2dfbf94f19',
  'attempt_ref': 'f5b4a62fc8ab7fce1429bcbe18b3efcdfb486529bda297fc3a441961e2d6f0d0',
  'original_sending_sha256': '4dec2bf3987896857ef134da08d609dbbd4dbded75455d6375bba79b49ff3d74',
  'unknown_row_sha256': '6e679ecad55278b10528df936df65909e857511f9dcee783befb32946c72b8f5',
  'terminal_links_sha256': '68f4b9401f2a7d18ec473e5af11dd7ec2dbb114c223ddd5ba0669d7a036ca65f',
  'job_metadata_sha256': '5e2d05d5c4a975428b3874bed9d48025b483be2a9735c0a40624ae65f5e0936f',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17659,
  'source_identity_sha256': 'e1a780bcd42fd5dfcc939cfb43572869f4cac41ef47e2538b4b72fee76de6b66',
  'source_pair_sha256': '3b1eb405fb55856f109af85096dfabd4e0c3a91b9bc8e82c25cef7d91d78f886',
  'number_sha256': '840933385dadc2a2e6fb3cb1320a2604b6122ddc98c351c02882798a8ae45ca4',
  'public_number_sha256': '978779ee394f6238869b288b6fc6c6118310f76f0db1b3725ab7a4b8d157c38b',
  'attempt_ref': 'bafb8d56eaacca04dcbd973977b12d42e0333f2789ff36ebef94e11bac4f2c78',
  'original_sending_sha256': '8baf8d37777324a51f9262c1d95c4623ba272b9cd021e5028fe215974673bbed',
  'unknown_row_sha256': 'b969d972171c1170d16cd66ffc75b9a6df9c91af07632dc4a9104959e5f040b8',
  'terminal_links_sha256': '9476744ca8349e7f9384019b2368c34b9b97f6214c2d3490a80fcb47622ff8e3',
  'job_metadata_sha256': '0ea6df8b0d7b9738b2f9c63b120d4e5278de87f5cadc0148254cc761d3f54aaf',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17660,
  'source_identity_sha256': '45f6e12b1c8a5f9ad9670ff1e5d9d0ba6508bf5a82c1debd5612b9840cd2e3db',
  'source_pair_sha256': '01eeb4e8756314b642546ab677f8c9222d318c680f4f06c251ba4162df70a207',
  'number_sha256': '0ab33f4451f249771ffe56475a6e58968df9a0e0591eb7091bc219608e9432fb',
  'public_number_sha256': 'd1fffc8b965c9216f35d2c2ddfc34786a2878d9cc9792908241eab85d66b83fe',
  'attempt_ref': 'e6c33f1322f70c72933a6f1f57c8aef39281e9e498f19f7ee8da4dc3bf0a5ea1',
  'original_sending_sha256': '365eef231150b717d8a3658f9cad60eece2b0afae3721187f6ab8c9a7779cad4',
  'unknown_row_sha256': '5f8d920dac015d7037d281c0e60aae2e51ace62a1aaad134e936fa9546c9be06',
  'terminal_links_sha256': '2486dfaee4b17323d88909588f41665a639dacb45a64d30a66d8fa747aaf3dd8',
  'job_metadata_sha256': 'b10fa518622c2c4e528b65783620b1c8187b2c0b97144ce8c1ec29f9b8505ff6',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17661,
  'source_identity_sha256': 'a3e6970941e7cfcc509e72afbec7cc7e53f08a95c92d9856aa144e96281986f8',
  'source_pair_sha256': '644ddeec93042e8ea65dbb087f37f6583d0ab30666e91b31c282e952908e862e',
  'number_sha256': 'f5dcf8ea7d9c157a6132e149d71232d79a55503642d119aaec36bced9f6505ab',
  'public_number_sha256': '692611b6e19704635defccff583f94033b12766d230949c21195c3b36a7228d3',
  'attempt_ref': '7a01b83decd3679ff0766b563478b9970b171238eb370f46937012ed7903ae80',
  'original_sending_sha256': '4129f3336c056f6c4eb9793412e1a21fe33259f8aefd4a96be62ccf1bcac656b',
  'unknown_row_sha256': 'df72891eaeead19952c8676d9702db3424a6b4378a221d4714a010bb7c73db69',
  'terminal_links_sha256': '53dc1264abe91b2e18e0c743790e91611ab04dccf06a87bc46707e2310ba9724',
  'job_metadata_sha256': '07ab76848185e723e594c9fca78adf8227d1fa71b29a9ca0ab1fe53244050be3',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17662,
  'source_identity_sha256': '855b0aacc5213b69ccae881d28c46338a3962dd74352979fdf22b7faf6f401da',
  'source_pair_sha256': 'b58f843e3350d99df1c695fc106c1fb0d7eeb4463e8824a57bbe2ccaf7ad83fe',
  'number_sha256': '7a20764deaaab21a11e015aab5ea16c722ed36ea519d72423aa768f07a4642b2',
  'public_number_sha256': 'a75d0754aad6f3de43249212fd1f2bb662676cdd623c10da36ed35c12a7edd25',
  'attempt_ref': '527db3d7a85c5764c735363bd23b28f614167f4c2e48dad7e879ef60128ea5f2',
  'original_sending_sha256': 'b50f26fc7b2bfd58992c49c292fe63d3a1fe3a9d2313144cba16b5eccb0d2e4a',
  'unknown_row_sha256': 'e92cc844641e1a519501d2509b6a04ad2787f4228b35168c40a5c00bf183e877',
  'terminal_links_sha256': 'a8a5f9865dc97d5bf759cd7ffeeb23c24e7842ca42654198f9c91a70b1547cf0',
  'job_metadata_sha256': '7e168ebc65f6f7d0cfdd3316ac2950788fd69c8ae3413e13010dd4ac0537f18e',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17663,
  'source_identity_sha256': '1166668953670419bfaba4d65204269b1513c1d197c174fcdedc6cfb9fd030f0',
  'source_pair_sha256': '4987e6f244b022a394f14719589b99ae0cf3aee90d366ef6c00f26f44a6d2929',
  'number_sha256': '04b53f64a91e1f169d100f80cb6f203727c757891bff391091c621bb0679f8bd',
  'public_number_sha256': '4a5822f6cf7c9c68995d2e2f42a38a6ecd4dae7421a355e69766d9ca1266b6c9',
  'attempt_ref': '888f433ed906955a5e1afdc0e580e8952d4489ed7118af43c283a9aaf3c301a0',
  'original_sending_sha256': '47486fa39b71e398015b317627a6d61222548f1c58c33b0e93bdcf3052a0b0d5',
  'unknown_row_sha256': '1c2c771e8c71cbad909484f722238561e4ec007861353819a25ab8736fe5f06a',
  'terminal_links_sha256': 'b79530c1660c3bb38a4f8e1fa73f53e3df340239a11cfd37df9a2b829276d437',
  'job_metadata_sha256': '7eda4cd384afeacfa9b533096fc4d4b61302ec719357587c055804801bf3a57e',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17664,
  'source_identity_sha256': '238da0d2e57625cc49f56deb960b6b1ee379e5c375b596068e65e36dfe3c2d33',
  'source_pair_sha256': '1abd2e5c83af423e7abdbb20ca1e7b7ecebc707d244b75908aa5a9101c0aacf5',
  'number_sha256': 'f71f6173c68d603dc1b072ac123840f14e605c65409c35d1bb83c1fbcedce0f5',
  'public_number_sha256': 'ae5b3441bdcd4f383f521505402bb1a4229b3ad52914a8e837f989c13a8b0b80',
  'attempt_ref': '22b312c213e32543abc3f61f6d1eab626cf17ee0d0b0ce4befa574116586e64c',
  'original_sending_sha256': '365a5e2206b3a1fd1a6172914daad56245d0e5e7abdabc7e89d49408d21150fa',
  'unknown_row_sha256': '2f69870ab98810e8d47f25e126d79eba907678def94a31798610c7ffe288b38d',
  'terminal_links_sha256': '65fed2650aede25c85649788b99dd48a03cbc63480233ef4990a49de7d417f2f',
  'job_metadata_sha256': 'ec54ab4fe67b0a542bd3aaa6b6e9de27c5361e5e97bff64ad9a294b9d4a1386a',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17665,
  'source_identity_sha256': '05adfcc1839decdd8832d27b4c26f55256360e9e4a68a84192380f1227aab1a9',
  'source_pair_sha256': 'eed432b5155d2a479e24536791405b0032a78cdb707c3353dfe50fd2540b1f64',
  'number_sha256': '80ad7c2c89bc0b4aa0e2a7bbedbf2b5c7047da3303d7442d84ab2130158999ad',
  'public_number_sha256': 'bd88021d3c49db8ead3091d4d37fc03c2fe28965cf34c475c28f9b3680bfae7f',
  'attempt_ref': '2da87615406aab22b3f2dc429c8431f49e3abaacd7b928bd2bf1b07851d28ad4',
  'original_sending_sha256': '2aae18ea81ce08f74d8d49239d001eef49c5147fe61986a56526169db9290c4e',
  'unknown_row_sha256': '15bdb312e228e6116ca6d60fb6e5e4feee0e1506a64f4ea56945e63dc09f264a',
  'terminal_links_sha256': '11c39a9f3521bad234ffcfff59d364e2dddf42c0a3bbccf4ee1ca8af92d0e423',
  'job_metadata_sha256': '850f50f47656de0f038ae8e32d9fde2a77bfcddeb8133f40480eeeda1ff7adca',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17666,
  'source_identity_sha256': 'b7d4513f37dbaf3605c347607f902a4f70b2637c3fc0b1eff8a1f30841941c65',
  'source_pair_sha256': 'b78d482a12e1253e6aa4614efe72679d268d4916119e40137c0099bf0033a483',
  'number_sha256': 'b76910051923dac056b1b4f924fe2f5d1d599b1e7e8c0e95a20a9a390f054e47',
  'public_number_sha256': '29d1dbad528195708859bcacf568bd3a6827d957fc56046b040b968b1064db61',
  'attempt_ref': 'b94c7adc47a21d2e3c9bed2a242733a8e3a0791d972f84598a17aee69bbe6b74',
  'original_sending_sha256': '76eb0f56c6f764b0c3fc8a911b9afc784b3506ba44eb8e843b7b37a1b679f95c',
  'unknown_row_sha256': 'fb2ebd006f433e9a62cf8fbf0aaccd5964af881064c361acbb02bae1ae70d66c',
  'terminal_links_sha256': 'a2cdd322f067367779afee9ed364084b949098be709a47fbfdbb69eb77079dbf',
  'job_metadata_sha256': '2d98a26187f836c8dba519627cc037395ef4c4997e20f34f7cf4dea9be4621ea',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17667,
  'source_identity_sha256': '46f053f10a1ba69b41fa75e645776f4c136827cb105a53d4c649ec61e7942eef',
  'source_pair_sha256': 'c92a2a0011d7963ba903536880471d3ca8dbc2dcc8da78d97cc83c854830f306',
  'number_sha256': '19f8c1c96c6e880980f72a5719f0f1a2477773d367d7999f0bebdd96b5c53210',
  'public_number_sha256': '4e82e5050f770daf6b66b2330554451ccefbc9263374ca73dac189dc726a8486',
  'attempt_ref': '37ed853119cd73c9a3bf238bf5349f330992b3fc6142cfdf13b6e05fc2b35820',
  'original_sending_sha256': '31c8aa206d4bbff574a329c86f4527c25cd4e5701dc3959407952a9550f29cd8',
  'unknown_row_sha256': '2ad861bbf6462fcce267d9bbe6d307e8a1f43b4b4fd677b41b692550af36e099',
  'terminal_links_sha256': '7a68c80b2f3187d7201061a5d58c32e435e41e43fbe7f1af189b0a5e64d5ad84',
  'job_metadata_sha256': '8f13c6f1c3f6e8a54f76d03609e32c4d858ddc75a217fca8f1d9359612e9d020',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17721,
  'source_identity_sha256': 'a69c715512247b54934e951e527a0aa1a0463efab3587d2c352f2f9e1cfe6505',
  'source_pair_sha256': '565794edeb2792bf75470ae51f514fc16d695de5e183b416e6b4f505678ab68f',
  'number_sha256': 'f902bb60141451c37792cf83a95687442108dd7813dd917a3b468f3597dc4ea4',
  'public_number_sha256': 'c09ef1d0fadc2f6795f99a54a62d7725f9e462356ceda71b170d80d8690b0267',
  'attempt_ref': 'b6b59396a4b4a5c0a3e8585e26a8bb13a3e57628ab8cb1396cc6799fd2f62957',
  'original_sending_sha256': '3bf73dfdbc77bd7845233937ca9bd18f758772370d7a4ebdfeb9eb54fea4f71a',
  'unknown_row_sha256': 'cd21a4fff2baa0a367224ed8ec184e0bd7f901967bdedb4a14274c2e8838d52e',
  'terminal_links_sha256': '9a6a0fa33d9b83903acecf6a4678a191e121eb6d912f35645731f7aec7f92f97',
  'job_metadata_sha256': '9472de9bea2b848b7b00271fa766d88d0c82bb98c2c8cda48613a6c15faa43e4',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17722,
  'source_identity_sha256': '9ccf1a6e1b5dc3a883ea1300005366981af7a02f0edf1b4f6adc65a56dbafe5e',
  'source_pair_sha256': 'd42601ab1892c1702df8fae555f3417f9060354aa53b280d4dfb5af2efd82a3f',
  'number_sha256': '3a208755652b83a3e72c6807948af20ffcb100ae4cabd24767e678ca0049d170',
  'public_number_sha256': '1ea61a1eeb2ebf5cf5f2c7b459d5627d4d0f8ab71a6e96173cf978d8d3c2208e',
  'attempt_ref': 'c38e896889a194fcdae0788f90ed696aa79a12b2de41c2fe8fc9bdf069f6c6bb',
  'original_sending_sha256': '2b1448d3cce227ca459f2661efacfe8534d7e335cbd603cdbf4aa6f5c9ca7719',
  'unknown_row_sha256': 'bb3a91aa9100a6ed7aae6baad959c2af3340c7e216ca0ea9fa30830eff0f85db',
  'terminal_links_sha256': 'b4a2b3821f1db95922021d4e5917ef4c1481e106e4d53f1f2e65596b9c3bd1f1',
  'job_metadata_sha256': '362a65fd5ef633e723170249a72987200631f9131c72b2a123f5d1e7db3fd346',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17723,
  'source_identity_sha256': '4d939e50003309cc92b4827a461396fa9d5fdb59dd061e8a9780b89af1c10e55',
  'source_pair_sha256': '16ed38d500b28c2a99c2b8ed19eb05e8f8d5a496037c848c056af2027dfe0e73',
  'number_sha256': '63835722f76a90061f446577c049f54efb2375edb2f23f86fe602b0d4b72ce53',
  'public_number_sha256': '1647057e032c66e4c49b134be94edb887ef98bd911e9762b35cbd7d8712b2510',
  'attempt_ref': 'f0a993b88624979497b31c94879f12b4e6bc4b88925306fbb599ceb2ec1475fd',
  'original_sending_sha256': '32fffc5793996227d994f5798ad000bd4cdeea4b5266e9d9dfe22f54690f2121',
  'unknown_row_sha256': 'ca19cd1ebfd0ee997fa526e0308a897913c0a7dc6247527359b65e8220fe6752',
  'terminal_links_sha256': 'f5cf628d99ff898724393ca200e6396c148ba42e330aaa9fd22b1a3a65f7c925',
  'job_metadata_sha256': 'a62060bc638c1dd687c4648c9530879e7ee54d906518fc36711232d6694cab89',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17724,
  'source_identity_sha256': '8a816b64334e7b0cf96d83b73dcca2d65e3fa932796987669122074a7a345ab3',
  'source_pair_sha256': 'de0c49382bb24582423668746527f8857aa66f1afb9367acd7d4d0331e91c61f',
  'number_sha256': '5785417b9c4f543da02ac0bb18e2a0f167819a89a9e3bfcc1069a3d4828fb755',
  'public_number_sha256': '17413312be72eea4500cc9f782536749b14c39675b0a66eda65663ab7a9d71c1',
  'attempt_ref': 'd0d7485abfc4ad821a8706a654a326bf2a5ea492a078aef95fead985cae93163',
  'original_sending_sha256': '4c929532cd609de783f9e5e3ea42bb8fa3417d018f0ee5d7cc5e2d5f5d5e6588',
  'unknown_row_sha256': 'da91675b958da925cf691f2dba7a40ef05bca0a7f5567fae8cae5abeb36a216a',
  'terminal_links_sha256': 'ff0b2e35a8e46a52633b9838f5c209dede4c3f5c1b034ec8ca3135f0e02c312c',
  'job_metadata_sha256': '84c7e89a3e0cedf89ff63e8f68a78ed7e5f37bbe306f8b838f6b70d79230573b',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17725,
  'source_identity_sha256': '2930c33ce20bbee817be516502c1ee1b85ca486a8f25491c7bc5d959815adc86',
  'source_pair_sha256': '0e568a5af2eced99bf268bee0c1ac729f94edb18950198562661d6490907c9b7',
  'number_sha256': '730aaac4b59596380b634d1861adb1d780acdc67ea10b15c00b6d9c7d63cae5b',
  'public_number_sha256': 'a37fc08efe9bfa9aad408845293d37df31847959f6dbf4b0730125980ba9df3f',
  'attempt_ref': '528d4504cb3d7a6bbe55fc769bef0d7673f03b417a02d171547c0aa6bad2cd4e',
  'original_sending_sha256': 'e0621bd6903832e0f4b24dc4db7d628f910950cca8cbf529c99afb2dff4518e0',
  'unknown_row_sha256': '6f57ce907d3b3a443a28df0665e03701555957167fcb4d1acc66e6527da0611a',
  'terminal_links_sha256': '6d33f8b7461c57302f327bac92b716f74974d28a4112f70c8260c1d3f6f5a771',
  'job_metadata_sha256': 'ee91d907a980f7966aee8d826220035aaf8914c6e74ce6ff063351a6a5444587',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17726,
  'source_identity_sha256': '7154deff8c68f6f8e4d6c8bb067acfd4a550481bd87ca2629aa64254501bbb50',
  'source_pair_sha256': '504a39eea5a4fa3f2e1c937c60c3a5e61addff4aec7a6ec7558073633d7839b7',
  'number_sha256': '34236ae3be398a7f9923cba148f9a92577ae1abcb3c43bd2dddb357a215196b6',
  'public_number_sha256': '3dee8ff6b160c43f26f353b52a90b2f190215c80536bc9cab0f8da225a9bfa41',
  'attempt_ref': 'd7451aee4366a1434ba3668e5debe870684e9dae0546fc73441c4718de15e59d',
  'original_sending_sha256': '9bc6331d6df1516aae8c255be5dcf3b9239b86b5bde39bb39db675fb8c7ac9a6',
  'unknown_row_sha256': 'a764d41fff96596f4e7277667a0ed7b39e5b027aa96a447b441895d483ae9bf7',
  'terminal_links_sha256': 'aac46c3ea12080100fc256a597e954207c026d3c855e1627ae9cc6a5d3a061f6',
  'job_metadata_sha256': '1b8764369e06cb1d915c411b7e663ca272b1bdde2967834e4324734c4daebcec',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17727,
  'source_identity_sha256': 'c1fab8583d7b2e5c3f9ab8f4a8e85b5d694a90c999e475f9693bfc19c71237aa',
  'source_pair_sha256': '0824a52947af66fcb9c114bc63d83a621e5fdf72d9c4961763c8d714e6a33ff3',
  'number_sha256': '960f338687236c158c46579e6187285f7747414976ae99dc43710c4c659da603',
  'public_number_sha256': '0e4ba23c6f5fc1db275f4918d7a8d867d2fb194bde01c22394f662bef8da870c',
  'attempt_ref': '0b50bfb5b250aa097f2d2368c8ed0ff991bea9c7d2e5d02873a0abdd6e1c9221',
  'original_sending_sha256': 'b74e8321c95b4e434bf8fd6155884bb7ad704af33b1e00d53cc9cbc49ca076ad',
  'unknown_row_sha256': 'cd0f915ee4d67ced2baf23d218a0e2a9f62a1b59024f65757fe758e43fcfc47c',
  'terminal_links_sha256': '321dee8e1f0799960718f5ec5dc2f0ec0f398ccfa17301291374c95b94f8a749',
  'job_metadata_sha256': 'c09d9f91bbbb56665640fdce02c7aa0bd3df12de1cc854e7ddc556a1ff7b9989',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17728,
  'source_identity_sha256': 'de49d3241acd1d963a49e843ebc3a08100c97a6358b10b58ed8a7d127a77f908',
  'source_pair_sha256': 'f3b24da904b8d44568b9a34b49065fffcc3a26d492162cf40b0b8c586e01c0c1',
  'number_sha256': '239afb2e7655fbae8ba937cf3394bfdd7b2ccc46a3e286274f7d61378f6990c9',
  'public_number_sha256': 'cf2fe58f31c4a23f24ae5e583d522a480ea471e35b2971432c54650dd8fdcedf',
  'attempt_ref': '91f645044a1c51b6d4810327d112b02592bad2d6314140ef762d8eee95920f76',
  'original_sending_sha256': '9e33329be4ad8cfcb4f2a7b3d16a98a207be361c1e2286a125623688104f8717',
  'unknown_row_sha256': 'f503047693166f80a043232a310700ec37861d30371006aeb236cb1a88a3a981',
  'terminal_links_sha256': '3a180241160bd05ff50a3b4b2d39a4dc3948cdcaf0f5f78ee149ae3cc247fe29',
  'job_metadata_sha256': 'c8adb2fd0c81ea069bb77bfe39efc7f6ea583074129436cb1114b4b3c94831fc',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17741,
  'source_identity_sha256': '2fe8dbed7ce9e457deec3d41e154743e92bf6fdb30205cf6658bdfef45804669',
  'source_pair_sha256': '7b84c526383b091fd2222952eda5333c53f3bfa51acbe271813b03875c63e9ec',
  'number_sha256': '2c9466c6d95734ad718c592e30f7c982e57748157931e8f81d1f35fa6b8db9a7',
  'public_number_sha256': '9550033f59e2671ab9f105e96cd74bda6efd5c9b668ba2b2861300e1cbd8b11c',
  'attempt_ref': '8af72be8b65edc8c15625bfabc3c6ad126e199226c33d14a9b1ac85bd15a32dc',
  'original_sending_sha256': '78febac318db58fa82197e0a7608e71f03c0a3d44f116daf6df1e4fc3aaba743',
  'unknown_row_sha256': 'd57e4bd597c8d3b42cb29e85bfd9271f54a3ad58941a802c9869dfa6761824b9',
  'terminal_links_sha256': 'c7e83124e2cfefe54518e7abd6b5e89b2d59e96fb2782daad3f93557e6441047',
  'job_metadata_sha256': '18958a292832faf12793f15c362c6cf9fd3f9307d46a93c6172ec27d799da5b8',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17742,
  'source_identity_sha256': '4e521fd0c87fc99bfe0dc6dfa0d20785e287f9b0bcddde7d5d301dfbb2a199e1',
  'source_pair_sha256': 'bb881286552dd3b4bf79f4065fcca89ab7d6f1ff63e9f2c7d46b1a6420a2a4f3',
  'number_sha256': 'c24d3a1400a5d7bf89785ea9da425a48cc0d32de42d4483ed1d5a8642f546c00',
  'public_number_sha256': '7f9dd8f17d710ee6e6688266a54b697b2390c5b7d7aae7c48dbca27db1ad1360',
  'attempt_ref': 'f4714ddef19a9edf63cc41f58cc2f2140bdc1d0e3d0d7399766ded23c14486f8',
  'original_sending_sha256': '87eb47fb2a10f13b509eb2af8efd6f4fe2bbb8c6b14581a46cf21138451c41b1',
  'unknown_row_sha256': '41148738b6b3882efff2b4b4d2bdefce23f5b56f0681d3375778effd0b85a541',
  'terminal_links_sha256': '3b2fccab8f069ae96398f4dddc38d8548692a5b55140517c7192313e20f3521b',
  'job_metadata_sha256': '2dd08579dfc88e03c623a866f56471ddb8383d879d11f71b764c70234c9e91a3',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17743,
  'source_identity_sha256': '279c8b9ffd99d63ead57977ad6ee39ea290a0db09faf76e76cd1436e55bce3e8',
  'source_pair_sha256': '5520f2b5c7bb490b62f4b11ca3bca344bd92d3724cd90d0750251e168bf1a4a6',
  'number_sha256': '2477e4717cd119faa77b446e0c38e6b5b431e101a911f766fc9082992e480147',
  'public_number_sha256': 'd6cbf299f4bef694255bdaf1d9d76f053761d8ca7b3d0f7cb508a07db9d84af8',
  'attempt_ref': 'afe7b179fb2c3ba58e7450d27bee3ba9ab1998614f6e91bddc6399e8a356783f',
  'original_sending_sha256': 'dd993cd4fffbbb015468d3430856a1bf5040f9b67c03d72f5d0816f487b9249d',
  'unknown_row_sha256': 'ad2879b6708029b195d8258b121f562017d481c1f860f60f85ef815dcca739d4',
  'terminal_links_sha256': '2b5a7c92adf189f1383366aba2c919b51b46c70e2753aabfae3a196ec3bc1eb4',
  'job_metadata_sha256': '0ce4690e42d1c416d25a54d09e80b0c4b1b94a5e4659d60fb736739b910fe8fc',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17744,
  'source_identity_sha256': '3c450446a99df08de33791736edf64d3afdcd64272404509fd07ae10f5069fea',
  'source_pair_sha256': 'ffbe1a59d1017d3332440f9c916a81f259350efac3a9202f43e1c78d9bef3ccd',
  'number_sha256': '7119e866addbf1e04f7d0f9da32322a345a7b2204e74944b32277210436adba3',
  'public_number_sha256': '254e957742526dc1964743f5251061b99b69d39dea234ec546aca011d8f2b05a',
  'attempt_ref': '54c2cc260880f0e0997605ee07e930e480bd1f97c040d42f9e5333a4af8e08ed',
  'original_sending_sha256': '675a4d08cc7e519e8e2ce549892bdbb575bc2cfccd0006ff71560e17d3670fa6',
  'unknown_row_sha256': 'd743924ac35654eeda6052a81358f3fd8facb3eacb783059e153f6a701dc5e42',
  'terminal_links_sha256': 'b457df4915e585923c243c49aef3a9e59cb48c22d5801a9eb0e64aea9c373dd6',
  'job_metadata_sha256': 'c7f046431ea6827d32432dab7436462c39bf5c5145c586ced09af414070953ac',
  '_scope': 'TIMEOUT36'},
 {'order_id': 17745,
  'source_identity_sha256': 'd153809ef19b8889795ec1964ac7e718e2641e7049bbf3d4fe002412471edfaa',
  'source_pair_sha256': '56e55665abd2e6723cda17da8257e0ee7d3343a7d5cc0d80183f6586e2484d7c',
  'number_sha256': '5f303b72797a2477c9ebe735e8e71f91f6774cdbe1f8b9408dc72f7eae58d313',
  'public_number_sha256': '1ed714dfa2462e294dd51a104036ab99efc6439231cf86baa6841545ffcfc5db',
  'attempt_ref': '8d4906204e568971ca594c75b9b44cfa1e2e561fa51e709eacd8758c31f1a584',
  'original_sending_sha256': '858008f504632fe7bb465441032fd161887ad22d5b966f6fd01e504de4ce969c',
  'unknown_row_sha256': 'a822c6dc6ae151120ce8f172d40ae6043a1cc0edba0f0956e35fe3fb4674b9e0',
  'terminal_links_sha256': '974729145ad8565c39f9d74e4f3ee286a578abfc45f5d3e44a85c0ac5b6a94e9',
  'job_metadata_sha256': 'eda6911b48666af6c413498e6494e769d19a85aecfbf26bdbdbbd038cdbf282c',
  '_scope': 'TIMEOUT36'})

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def scope():
    if len(SCOPE)!=36 or {p['order_id'] for p in SCOPE}!=set(APPROVED_ORDER_IDS):
        raise ValueError('TIMEOUT36_SCOPE_INVALID')
    for key in ('source_identity_sha256','source_pair_sha256','number_sha256','attempt_ref'):
        if len({p[key] for p in SCOPE})!=36 or any(not re.fullmatch('[a-f0-9]{64}',p[key]) for p in SCOPE):
            raise ValueError('TIMEOUT36_SCOPE_INVALID')
    return SCOPE

def deny(record,channel):
    from six_unknown_receipt_hold import ReceiptReplayHeld
    reason='TIMEOUT36_ORIGINAL_ORDER_RECEIPT_HELD'
    logging.getLogger(__name__).warning('%s source=%s channel=%s attempt_created=false',reason,record['source_identity_sha256'][:16],channel)
    raise ReceiptReplayHeld(reason)

def guard_direct(facts,order_id=None):
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    source=hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest() if all(k in job for k in ('namespace','chat_id','message_id')) else None
    numbers={hashlib.sha256(str(n).encode()).hexdigest() for n in facts.get('order_numbers',[])}
    for p in scope():
        if source==p['source_identity_sha256'] or str(order_id)==str(p['order_id']) or numbers & {p['number_sha256'],p.get('public_number_sha256')}:
            deny(p,'direct_order_success')

def held_recovery(event_key,chat,message):
    pair=hashlib.sha256(f'{chat}:{message}'.encode()).hexdigest()
    for p in scope():
        if pair==p['source_pair_sha256'] or str(event_key).startswith('final:'+p['source_identity_sha256']+':'):
            return p
    return None

def guard_transport(data,policy):
    if policy.get('order_receipt') is not True:return
    reply=data.get('reply_parameters') or {}
    if isinstance(reply,str):
        try:reply=json.loads(reply)
        except ValueError:reply={}
    message=reply.get('message_id') if isinstance(reply,dict) else getattr(reply,'message_id',None)
    pin=held_recovery('',data.get('chat_id'),message or data.get('reply_to_message_id'))
    if pin:deny(pin,'order_receipt_transport')
    from execution_safety import CURRENT
    job=(CURRENT.get() or {}).get('job') or {}
    if all(k in job for k in ('namespace','chat_id','message_id')):
        source=hashlib.sha256(f"{job['namespace']}:{job['chat_id']}:{job['message_id']}".encode()).hexdigest()
        for pin in scope():
            if source==pin['source_identity_sha256']:deny(pin,'order_receipt_transport')

def job_metadata(connection,source):
    # Server verification reads only metadata plus a ciphertext digest.
    connection.create_function('receipt_payload_sha256',1,lambda x:hashlib.sha256(str(x).encode()).hexdigest(),deterministic=True)
    rows=connection.execute('SELECT namespace,chat_id,message_id,fingerprint,state,reason,attempts,received_at,started_at,finished_at,lease_until,lease_token IS NOT NULL lease_token_present,receipt_payload_sha256(payload) payload_sha256 FROM telegram_order_jobs WHERE namespace=? AND chat_id=? AND message_id=?',source).fetchall()
    if len(rows)!=1:raise ValueError('TIMEOUT36_JOB_NOT_UNIQUE')
    return dict(rows[0])

def partition_verified(unresolved,connection):
    """Independent proof-of-non-replay review, never proof of original ACK."""
    from frozen_direct_receipts import terminal_links
    pins={p['attempt_ref']:p for p in scope()}
    selected={hashlib.sha256(x['attempt_id'].encode()).hexdigest():x for x in unresolved if hashlib.sha256(x['attempt_id'].encode()).hexdigest() in pins}
    if set(selected)!=set(pins):raise ValueError('TIMEOUT36_EXACT_SET_CHANGED')
    for key,row in selected.items():
        pin=pins[key]
        if row['status']!='UNKNOWN' or digest(row)!=pin['unknown_row_sha256']:
            raise ValueError('TIMEOUT36_ORIGINAL_ROW_CHANGED')
        if digest(terminal_links(connection,row))!=pin['terminal_links_sha256']:
            raise ValueError('TIMEOUT36_TERMINAL_LINKS_CHANGED')
        order=connection.execute('SELECT source_chat_id,source_message_id,order_number,business_order_number FROM orders WHERE id=?',(pin['order_id'],)).fetchone()
        if order is None or hashlib.sha256(str(order[2]).encode()).hexdigest()!=pin['number_sha256'] or hashlib.sha256(f'{order[0]}:{order[1]}'.encode()).hexdigest()!=pin['source_pair_sha256']:
            raise ValueError('TIMEOUT36_SOURCE_CHANGED')
        public=hashlib.sha256(str(order[3]).encode()).hexdigest() if order[3] else None
        if public!=pin['public_number_sha256']:raise ValueError('TIMEOUT36_SOURCE_CHANGED')
        links=connection.execute('SELECT namespace,chat_id,message_id FROM telegram_execution_commits WHERE order_id=?',(pin['order_id'],)).fetchall()
        if len(links)!=1:raise ValueError('TIMEOUT36_COMMIT_NOT_UNIQUE')
        if digest(job_metadata(connection,tuple(links[0])))!=pin['job_metadata_sha256']:
            raise ValueError('TIMEOUT36_JOB_CHANGED')
        if connection.execute('SELECT 1 FROM telegram_execution_receipts WHERE chat_id=? AND source_message_id=? LIMIT 1',(order[0],order[1])).fetchone():
            raise ValueError('TIMEOUT36_RECOVERY_ROW_PRESENT')
    path=Path(os.getenv('RAINBOW_RECEIPT_AUDIT_PATH','success_receipt_audit.jsonl'))
    if path.stat().st_size>32*1024*1024:raise ValueError('TIMEOUT36_AUDIT_TOO_LARGE')
    chains={k:[] for k in pins}
    with path.open() as stream:
        fcntl.flock(stream,fcntl.LOCK_SH|fcntl.LOCK_NB)
        for line in stream:
            row=json.loads(line);key=hashlib.sha256(row['attempt_id'].encode()).hexdigest()
            if key in pins:chains[key].append(row)
            elif any(row.get('task_id')==p['source_identity_sha256'][:16] or any(hashlib.sha256(str(n).encode()).hexdigest() in {p['number_sha256'],p['public_number_sha256']} for n in row.get('order_numbers',[])) for p in pins.values()):
                raise ValueError('TIMEOUT36_NEW_ATTEMPT_FOR_SOURCE')
    for key,chain in chains.items():
        if len(chain)!=2 or [r['status'] for r in chain]!=['SENDING','UNKNOWN'] or digest(chain[0])!=pins[key]['original_sending_sha256'] or digest(chain[1])!=pins[key]['unknown_row_sha256']:
            raise ValueError('TIMEOUT36_HISTORY_CHANGED')
    accepted={x['attempt_id'] for x in selected.values()}
    return [x for x in unresolved if x['attempt_id'] not in accepted],list(accepted)
