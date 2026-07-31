#include "Arduino_LED_Matrix.h"
#include <Arduino_RouterBridge.h>

Arduino_LED_Matrix matrix;

static const int MATRIX_COLS = 13;
static const int MATRIX_ROWS = 8;
static const int BARS_COUNT = 5;
static const int BARS_COLS = 5;
static const int BARS_START_COL = 8;
static const int GAP_START_COL = 6;
static const int COUNT_COLS = 6;
static const int MAX_LEVEL = 7;

static const int SERIAL_BAUD = 115200;

uint8_t frame[MATRIX_ROWS][MATRIX_COLS];

uint8_t digitPatterns[10][5] = {
  {0b111, 0b101, 0b101, 0b101, 0b111}, // 0
  {0b010, 0b110, 0b010, 0b010, 0b111}, // 1
  {0b111, 0b001, 0b111, 0b100, 0b111}, // 2
  {0b111, 0b001, 0b111, 0b001, 0b111}, // 3
  {0b101, 0b101, 0b111, 0b001, 0b001}, // 4
  {0b111, 0b100, 0b111, 0b001, 0b111}, // 5
  {0b111, 0b100, 0b111, 0b101, 0b111}, // 6
  {0b111, 0b001, 0b010, 0b010, 0b010}, // 7
  {0b111, 0b101, 0b111, 0b101, 0b111}, // 8
  {0b111, 0b101, 0b111, 0b001, 0b111}  // 9
};

int nodeCount = 5;
int rssiLevels[BARS_COUNT] = {0, 0, 0, 0, 0};
volatile bool displayDirty = false;

char inputBuffer[64];
int inputLen = 0;

void clearFrame() {
  for (int r = 0; r < MATRIX_ROWS; r++) {
    for (int c = 0; c < MATRIX_COLS; c++) {
      frame[r][c] = 0;
    }
  }
}

void setPixel(int col, int row) {
  if (col >= 0 && col < MATRIX_COLS && row >= 0 && row < MATRIX_ROWS) {
    frame[row][col] = 1;
  }
}

void drawDigit(int col, int row, int digit) {
  if (digit < 0 || digit > 9) return;
  for (int r = 0; r < 5; r++) {
    for (int c = 0; c < 3; c++) {
      if ((digitPatterns[digit][r] >> (2 - c)) & 1) {
        setPixel(col + c, row + r);
      }
    }
  }
}

void drawNodeCount(int count) {
  int d = constrain(count, 0, 9);
  int x = 1;
  int y = 1;
  drawDigit(x, y, d);
}

void drawSignalBar(int barCol, int level) {
  int x = BARS_START_COL + barCol;
  int lvl = constrain(level, 0, MAX_LEVEL);
  for (int r = MATRIX_ROWS - lvl; r < MATRIX_ROWS; r++) {
    setPixel(x, r);
  }
}

void drawSignalBars() {
  for (int i = 0; i < BARS_COUNT; i++) {
    drawSignalBar(i, rssiLevels[i]);
  }
}

void renderAll() {
  clearFrame();
  drawNodeCount(nodeCount);
  drawSignalBars();
  matrix.renderBitmap(frame, MATRIX_ROWS, MATRIX_COLS);
}

bool parseLine(const char *line) {
  int cnt = 0;
  int levels[BARS_COUNT] = {0, 0, 0, 0, 0};
  int n = sscanf(line, "%d,%d,%d,%d,%d,%d",
                 &cnt,
                 &levels[0], &levels[1], &levels[2],
                 &levels[3], &levels[4]);
  if (n < 1) return false;

  nodeCount = constrain(cnt, 0, 9);
  for (int i = 0; i < BARS_COUNT; i++) {
    rssiLevels[i] = constrain(levels[i], 0, MAX_LEVEL);
  }
  return true;
}

bool receiveMeshUpdate(String line) {
  if (!parseLine(line.c_str())) {
    return false;
  }
  displayDirty = true;
  return true;
}

void setup() {
  Bridge.begin(SERIAL_BAUD);
  Bridge.provide("mesh_update", receiveMeshUpdate);
  matrix.begin();
  renderAll();
}

void loop() {
  if (displayDirty) {
    displayDirty = false;
    renderAll();
  }
  delay(10);
}
