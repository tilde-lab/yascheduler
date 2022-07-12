// g++ -o dummyengine dummyengine.cpp
// i686-w64-mingw32-c++ -o dummyengine.exe dummyengine.cpp

#include <cstring>
#include <fstream>
#include <unistd.h>

using namespace std;

#if defined(MINGW) || defined(__MINGW32__) || defined(__MINGW64__)
#include <time.h>
int _dowildcard = -1; /* enable wildcard expansion for mingw */
#endif

const char FILENAME_SUFFIX[] = ".out";

void process_file(char in_fname[]) {
  // copy input filename to output filename with suffix
  char *out_fname = new char[strlen(in_fname) + strlen(FILENAME_SUFFIX)];
  strcpy(out_fname, in_fname);
  strcat(out_fname, FILENAME_SUFFIX);

  printf("processing arg as a file: %s\n", in_fname);
  printf("filename to be written: %s\n", out_fname);

  // copy input file contents to output file
  ifstream in_file(in_fname);
  if (!in_file.is_open()) {
    printf("ERR can't open %s file for reading\n", in_fname);
    exit(1);
  }
  ofstream out_file(out_fname);
  if (!out_file.is_open()) {
    printf("ERR can't open %s file for writing\n", out_fname);
    exit(1);
  }
  out_file << in_file.rdbuf();
  in_file.close();
  out_file.close();

  delete[] out_fname;
}

int main(int argc,     // number of strings in array argv
         char *argv[]) // array of command-line argument strings
{
  printf("Dummy engine output\n");

  int i;

  for (i = 1; i < argc; i++) {
    process_file(argv[i]);
  }

  // sleep some time
  srand(time(NULL));
  int sleep_time = rand() % 8;
  printf("sleeping %d seconds\n", sleep_time);
  sleep(sleep_time);

  return 0;
}
