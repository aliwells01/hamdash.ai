// CUT START
var disableSetup = false; // Manually set to true to disable setup page menu option
var topBarCenterText = "W5ALI";

// Grid layout desired
var layout_cols = 3;
var layout_rows = 3;

// Menu items
// Structure is as follows: HTML Color code, Option, target URL, scaling 1=Original Size, side (optional, nothing is Left, "R" is Right)
// The values are [color code, menu text, target link, scale factor, side],
// add new lines following the structure for extra menu options. The comma at the end is important!
var aURL = [
  [
    "#2196f3",
    "MyPoTA",
    "https://aliwells01.github.io/hamdash.ai/pota-cards-mock.html",
    null,
    "L"
  ],
  [
    "#058019",
    "WRL",
    "https://app.worldradioleague.com/login",
    1,
    "L"
  ],
  [
    "#8d6f02",
    "HF Real Time",
    "https://hf.dxview.org/",
    null,
    "L"
  ],
  [
    "#000000",
    "HAMCLOCK",
    "http://10.0.0.119:8081/live.html",
    null,
    "L"
  ]
];

// Feed items
// Structure is as follows: target URL
// The values are [target link]
var aRSS = [];

// Dashboard Tiles items
// Tile Structure is Title, Source URL
// To display a website on the tiles use "iframe|" keyword before the tile URL
// [Title, Source URL],
// the comma at the end is important!
var aIMG = [
  [
    "RADAR",
    "https://www.arrl.org/images/view//Charts/Band_Chart_Image_for_ARRL_Web.jpg"
  ],
  [
    "LOCAL RADAR",
    "https://radar.weather.gov/ridge/standard/KFFC_loop.gif"
  ],
  [
    "HAMCLOCK",
    "http://10.0.0.119:8080/get_capture.bmp"
  ],
  [
    "ISS POSITION",
    "https://www.heavens-above.com/orbitdisplay.aspx?icon=iss&width=600&height=300&mode=M&satid=25544"
  ],
  [
    "Greyline",
    "https://www.timeanddate.com/scripts/sunmap.php?iso=now"
  ],
  [
    "",
    "https://www.hamqsl.com/solar101vhf.php"
  ],
  [
    "",
    "https://prop.kc2g.com/renders/current/fof2-normal-now.svg"
  ],
  [
    "",
    "https://www.dxmaps.com/"
  ],
  [
    "",
    ""
  ]
];

// Image rotation intervals in milliseconds per tile - If the line below is commented, tiles will be rotated every 5000 milliseconds (5s)
var tileDelay = [
  11200,
  10000,
  11000,
  10100,
  5000,
  10000,
  5000,
  5000,
  5000
];

// CUT END
