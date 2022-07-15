
// g++ -o dummyengine dummyengine.cpp
// i686-w64-mingw32-c++ -o dummyengine.exe dummyengine.cpp

#include <cstring>
#include <iostream>
#include <fstream>
#include <unistd.h>
#include <time.h>
#include <stdlib.h>
using namespace std;

int main(int argc, char *argv[])
{
    printf("Dummy engine output\n");

    int i;

    for (i = 1; i < argc; i++) {
        FILE* f = fopen(argv[i], "r");
        printf("processing arg as a file: %s\n", argv[i]);

        fseek(f, 0, SEEK_END);
        size_t size = ftell(f);

        char* content = new char[size];
        char* outfname = new char[256];

        rewind(f);
        fread(content, sizeof(char), size, f);

        strcpy(outfname, argv[i]);
        strcat(outfname, ".out");

        printf("filename to be written: %s\n", outfname);

        ofstream output(outfname, ios::out | ios::binary);
        output.write(content, size);
        output.close();
        delete[] content;
        delete[] outfname;
    }

srand(time(NULL));
int sleep_time = rand() % 60;
printf("sleeping %d seconds\n", sleep_time);
sleep(sleep_time);

return 0;
}
